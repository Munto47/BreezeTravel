from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DEFAULT_BASELINE = BACKEND_ROOT / "eval_data" / "g04_screenshot" / "licensed_baseline_v1.json"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.trip_understanding.full_text import (  # noqa: E402
    ControlledSnapshotPlaceResolver,
    DeterministicTextInferenceProvider,
)
from app.trip_understanding.pipeline import (  # noqa: E402
    TripUnderstandingPipeline,
    is_atomic_planned_place,
)
from app.trip_understanding.screenshot_batch.security import (  # noqa: E402
    secure_owner_only,
)
from app.trip_understanding.screenshot_ocr import (  # noqa: E402
    PaddleOcrAdapter,
    ScreenshotOcrRunSpecV1,
    StagedScreenshotAsset,
    extract_screenshot_document,
)
from app.trip_understanding.screenshot_ocr.models import (  # noqa: E402
    ScreenshotSourceDocumentV1,
    ScreenshotSourceLineV1,
)
from evals.g04_screenshot.baseline import (  # noqa: E402
    G04FrozenFieldV1,
    G04LicensedScreenshotBaselineV1,
    G04LicensedScreenshotCaseV1,
    canonical_sha256,
    oracle_projection_payload,
)
from evals.g04_screenshot.contracts import (  # noqa: E402
    G04EvaluatedPlaceMetricV1,
    G04NotApplicablePlaceMetricV1,
    G04PerformanceEvidenceV1,
    G04PlaceOracleItemV1,
    G04ScreenshotParityCaseV1,
    G04ScreenshotParityManifestV1,
    G04SeriousErrorV1,
    G04SourceEvidenceV1,
)
from evals.g04_screenshot.scorer import (  # noqa: E402
    G04ScreenshotManifestError,
    score_g04_screenshot_manifest,
)
from governance.g04_screenshot_parity import (  # noqa: E402
    FORMAL_RECEIPT_PATH,
    G04ParityReceiptError,
    _validate_formal_receipt,
    canonical_receipt_hash,
)


EXPECTED_PADDLEOCR_VERSION = "3.7.0"
EXPECTED_PADDLEPADDLE_VERSION = "3.3.1"
RUN_SPEC = ScreenshotOcrRunSpecV1(
    max_concurrency=1,
    per_image_timeout_seconds=15.0,
    batch_timeout_seconds=45.0,
    low_confidence_threshold=0.85,
)
GIT_COMMIT_HEX_LENGTH = 40
PRODUCT_ROOTS = (
    "backend/app/",
    "frontend/src/",
    "miniapp/src/",
    "packages/trip-check-client/src/",
)
PRODUCT_CONFIG_PATHS = {
    ".env.example",
    "backend/requirements-base.txt",
    "backend/requirements.txt",
    "docker-compose.yml",
    "frontend/next.config.js",
    "frontend/package-lock.json",
    "frontend/package.json",
    "miniapp/package-lock.json",
    "miniapp/package.json",
    "packages/trip-check-client/package-lock.json",
    "packages/trip-check-client/package.json",
}


class FormalGateError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FieldObservation:
    expected_key_fields: tuple[str, ...]
    observed_key_fields: tuple[str, ...]
    expected_reading_order: tuple[str, ...]
    observed_reading_order: tuple[str, ...]
    expected_low_confidence_fields: tuple[str, ...]
    observed_confirmation_fields: tuple[str, ...]

    @property
    def signature(self) -> str:
        return canonical_sha256(
            {
                "expected_key_fields": self.expected_key_fields,
                "observed_key_fields": self.observed_key_fields,
                "expected_reading_order": self.expected_reading_order,
                "observed_reading_order": self.observed_reading_order,
                "expected_low_confidence_fields": self.expected_low_confidence_fields,
                "observed_confirmation_fields": self.observed_confirmation_fields,
            }
        )


@dataclass(frozen=True)
class OcrSuiteResult:
    document: ScreenshotSourceDocumentV1
    observations: tuple[FieldObservation, ...]
    durations_ms: tuple[float, ...]
    staged_cleanup_count: int
    measured_output_signature: str


class ConcurrencyProbeOcrEngine:
    """Measure real adapter concurrency without changing recognition output."""

    def __init__(self, delegate: PaddleOcrAdapter) -> None:
        self._delegate = delegate
        self._state_lock = asyncio.Lock()
        self.active = 0
        self.max_active = 0
        self.name = delegate.name
        self.version = delegate.version

    @property
    def binding(self):
        return self._delegate.binding

    async def recognize(self, image_path: Path):
        async with self._state_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            return await self._delegate.recognize(image_path)
        finally:
            async with self._state_lock:
                self.active -= 1

    def close(self) -> None:
        self._delegate.close()


class CountingPlaceResolver:
    def __init__(self) -> None:
        self._delegate = ControlledSnapshotPlaceResolver()
        self.calls: list[tuple[str, str]] = []

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ):
        self.calls.append((city, atomic_place_name))
        return await self._delegate.resolve(
            city=city,
            atomic_place_name=atomic_place_name,
            category_hint=category_hint,
        )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_external_path(path: Path, *, code: str) -> Path:
    resolved = path.resolve()
    if _inside(REPOSITORY_ROOT.resolve(), resolved):
        raise FormalGateError(code)
    return resolved


def _git(*args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if process.returncode != 0:
        raise FormalGateError("GIT_READBACK_FAILED")
    return process.stdout.strip()


def _validate_clean_candidate(candidate_commit: str) -> str:
    normalized = candidate_commit.strip().lower()
    if len(normalized) != GIT_COMMIT_HEX_LENGTH or any(character not in "0123456789abcdef" for character in normalized):
        raise FormalGateError("INVALID_CANDIDATE_COMMIT")
    head = _git("rev-parse", "HEAD").lower()
    if head != normalized:
        raise FormalGateError("CANDIDATE_COMMIT_NOT_HEAD")
    if _git("status", "--porcelain=v1", "--untracked-files=all"):
        raise FormalGateError("CANDIDATE_WORKTREE_NOT_CLEAN")
    return head


def _cross_platform_product_fingerprint(candidate_commit: str) -> str:
    names = _git("ls-tree", "-r", "--name-only", candidate_commit).splitlines()
    paths = sorted(
        path
        for path in names
        if path in PRODUCT_CONFIG_PATHS or any(path.startswith(prefix) for prefix in PRODUCT_ROOTS)
    )
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        blob_id = _git("rev-parse", f"{candidate_commit}:{relative}")
        digest.update(blob_id.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _load_baseline(path: Path) -> G04LicensedScreenshotBaselineV1:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        baseline = G04LicensedScreenshotBaselineV1.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise FormalGateError("INVALID_FROZEN_BASELINE") from exc
    if baseline.review.is_frozen:
        artifact_path = REPOSITORY_ROOT / baseline.review.adjudication_artifact_path
        try:
            artifact_bytes = artifact_path.read_bytes()
            artifact = json.loads(artifact_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FormalGateError("ORACLE_ADJUDICATION_ARTIFACT_INVALID") from exc
        if _sha256_bytes(artifact_bytes) != baseline.review.adjudication_artifact_sha256:
            raise FormalGateError("ORACLE_ADJUDICATION_ARTIFACT_HASH_MISMATCH")
        adjudicator = next(
            item
            for item in baseline.review.bindings
            if item.role == "G04_ORACLE_INDEPENDENT_ADJUDICATOR"
        )
        transcribers = sorted(
            (
                item
                for item in baseline.review.bindings
                if "VISUAL_TRANSCRIBER" in item.role
            ),
            key=lambda item: item.task_id,
        )
        if (
            artifact.get("schema_version") != "g04-adjudicated-oracle-v1"
            or artifact.get("evidence_label") != "MULTI_AGENT_SIMULATED_REVIEW"
            or artifact.get("human_evidence") is not False
            or artifact.get("adjudication_payload_sha256")
            != adjudicator.output_sha256
            or artifact.get("transcriber_output_sha256s")
            != [item.output_sha256 for item in transcribers]
        ):
            raise FormalGateError("ORACLE_ADJUDICATION_BINDING_MISMATCH")
        projection = artifact.get("oracle_projection")
        if (
            not isinstance(projection, list)
            or canonical_sha256(projection)
            != baseline.review.oracle_projection_sha256
            or projection != oracle_projection_payload(baseline.cases)
        ):
            raise FormalGateError("ORACLE_PROJECTION_MISMATCH")
    render_cases = [case for case in baseline.cases if case.render_provenance is not None]
    for case in render_cases:
        provenance = case.render_provenance
        assert provenance is not None
        render_path = REPOSITORY_ROOT / provenance.render_script_repository_path
        if not render_path.is_file() or _sha256_file(render_path) != provenance.render_script_sha256:
            raise FormalGateError("RENDER_SCRIPT_BINDING_MISMATCH")
    for case in baseline.synthetic_performance_cases:
        render_path = REPOSITORY_ROOT / case.render_script_repository_path
        if not render_path.is_file() or _sha256_file(render_path) != case.render_script_sha256:
            raise FormalGateError("PERFORMANCE_RENDER_SCRIPT_BINDING_MISMATCH")
    return baseline


def _require_formal_evaluable_baseline(
    baseline: G04LicensedScreenshotBaselineV1,
) -> None:
    if baseline.parity_status == "PENDING_CAPTURE":
        raise FormalGateError("PENDING_CAPTURE")
    if baseline.parity_status == "PENDING_FRESH_REVIEW":
        raise FormalGateError("PENDING_FRESH_HASH_BOUND_REVIEW")
    if baseline.parity_status == "NOT_EVALUABLE_DERIVED_ONLY":
        raise FormalGateError("DERIVED_TEXT_SCREENSHOT_NOT_EVALUABLE")
    if any(
        case.evidence_tier != "LICENSED_REAL_SCREENSHOT" or case.case_status != "FROZEN_HASH_BOUND"
        for case in baseline.cases
    ):
        raise FormalGateError("LICENSED_REAL_SCREENSHOT_BASELINE_NOT_FROZEN")
    if not baseline.review.is_frozen:
        raise FormalGateError("PENDING_FRESH_HASH_BOUND_REVIEW")


def _image_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
        if len(header) == 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
            if header[12:16] != b"IHDR":
                raise FormalGateError("SOURCE_IMAGE_INVALID_IHDR")
            return struct.unpack(">II", header[16:24])
        if header[:2] != b"\xff\xd8":
            raise FormalGateError("SOURCE_IMAGE_FORMAT_NOT_PNG_OR_JPEG")
        stream.seek(2)
        while True:
            marker_prefix = stream.read(1)
            if not marker_prefix:
                break
            if marker_prefix != b"\xff":
                continue
            marker = stream.read(1)
            while marker == b"\xff":
                marker = stream.read(1)
            if not marker or marker in {b"\xd8", b"\xd9"}:
                continue
            length_bytes = stream.read(2)
            if len(length_bytes) != 2:
                break
            segment_length = struct.unpack(">H", length_bytes)[0]
            if segment_length < 2:
                break
            if marker[0] in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                payload = stream.read(5)
                if len(payload) != 5:
                    break
                height, width = struct.unpack(">HH", payload[1:5])
                return width, height
            stream.seek(segment_length - 2, 1)
    raise FormalGateError("SOURCE_IMAGE_DIMENSIONS_UNREADABLE")


def _validate_source_images(
    source_root: Path,
    cases: Sequence[Any],
) -> tuple[Path, ...]:
    if not source_root.is_dir() or source_root.is_symlink():
        raise FormalGateError("SOURCE_ROOT_INVALID")
    secure_owner_only(source_root, is_directory=True)
    expected_names = {case.source_file for case in cases}
    observed_names = {item.name for item in source_root.iterdir()}
    if observed_names != expected_names:
        raise FormalGateError("SOURCE_ROOT_NOT_EXACT_FROZEN_SET")
    paths: list[Path] = []
    for case in cases:
        path = source_root / case.source_file
        if not path.is_file() or path.is_symlink():
            raise FormalGateError("SOURCE_IMAGE_INVALID")
        secure_owner_only(path, is_directory=False)
        stat = path.stat()
        if stat.st_size != case.source_size_bytes:
            raise FormalGateError("SOURCE_IMAGE_SIZE_MISMATCH")
        if _sha256_file(path) != case.source_sha256:
            raise FormalGateError("SOURCE_IMAGE_HASH_MISMATCH")
        if _image_dimensions(path) != (case.image_width, case.image_height):
            raise FormalGateError("SOURCE_IMAGE_DIMENSION_MISMATCH")
        paths.append(path)
    return tuple(paths)


def _tree_sha256(path: Path) -> str:
    if not path.is_dir() or path.is_symlink():
        raise FormalGateError("MODEL_DIRECTORY_INVALID")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files or any(item.is_symlink() for item in files):
        raise FormalGateError("MODEL_TREE_INVALID")
    digest = hashlib.sha256()
    for item in files:
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(item)))
        digest.update(b"\0")
    return digest.hexdigest()


def _package_version(*names: str) -> tuple[str, str]:
    for name in names:
        try:
            return name, importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise FormalGateError("PADDLE_RUNTIME_PACKAGE_MISSING")


def _runtime_binding() -> dict[str, Any]:
    ocr_distribution, ocr_version = _package_version("paddleocr")
    paddle_distribution, paddle_version = _package_version(
        "paddlepaddle-gpu",
        "paddlepaddle",
    )
    if ocr_version != EXPECTED_PADDLEOCR_VERSION:
        raise FormalGateError("PADDLEOCR_VERSION_MISMATCH")
    if paddle_version != EXPECTED_PADDLEPADDLE_VERSION:
        raise FormalGateError("PADDLEPADDLE_VERSION_MISMATCH")
    gpu_process = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if gpu_process.returncode != 0 or not gpu_process.stdout.strip():
        raise FormalGateError("GPU_BINDING_UNAVAILABLE")
    first_gpu = gpu_process.stdout.strip().splitlines()[0]
    parts = [part.strip() for part in first_gpu.split(",", maxsplit=1)]
    if len(parts) != 2:
        raise FormalGateError("GPU_BINDING_INVALID")
    probe_code = (
        "import ctypes,json,paddle;"
        "lib=ctypes.CDLL('cudnn64_9.dll');"
        "lib.cudnnGetVersion.restype=ctypes.c_size_t;"
        "print(json.dumps({"
        "'compiled_with_cuda':paddle.device.is_compiled_with_cuda(),"
        "'compiled_cuda':paddle.version.cuda(),"
        "'compiled_cudnn':paddle.version.cudnn(),"
        "'runtime_cudnn_raw':lib.cudnnGetVersion(),"
        "'cuda_device_count':paddle.device.cuda.device_count(),"
        "'cuda_device_name':paddle.device.cuda.get_device_name(0),"
        "'cuda_capability':paddle.device.cuda.get_device_capability(0)"
        "},sort_keys=True))"
    )
    probe_process = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if probe_process.returncode != 0:
        raise FormalGateError("PADDLE_CUDA_RUNTIME_PROBE_FAILED")
    try:
        probe = json.loads(probe_process.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise FormalGateError("PADDLE_CUDA_RUNTIME_PROBE_INVALID") from exc
    if probe.get("compiled_with_cuda") is not True or probe.get("cuda_device_count", 0) < 1:
        raise FormalGateError("PADDLE_CUDA_RUNTIME_UNAVAILABLE")
    runtime_cudnn_raw = int(probe["runtime_cudnn_raw"])
    runtime_cudnn = f"{runtime_cudnn_raw // 10000}.{(runtime_cudnn_raw % 10000) // 100}.{runtime_cudnn_raw % 100}"
    compiled_cudnn = str(probe["compiled_cudnn"])
    return {
        "paddleocr_distribution": ocr_distribution,
        "paddleocr_version": ocr_version,
        "paddle_distribution": paddle_distribution,
        "paddlepaddle_version": paddle_version,
        "gpu_name": parts[0],
        "gpu_driver_version": parts[1],
        "paddle_compiled_cuda_version": str(probe["compiled_cuda"]),
        "paddle_compiled_cudnn_version": compiled_cudnn,
        "loaded_cudnn_runtime_version": runtime_cudnn,
        "cudnn_major_abi_match": (compiled_cudnn.split(".", maxsplit=1)[0] == runtime_cudnn.split(".", maxsplit=1)[0]),
        "cuda_device_name": str(probe["cuda_device_name"]),
        "cuda_device_count": int(probe["cuda_device_count"]),
        "cuda_compute_capability": list(probe["cuda_capability"]),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
    }


def _normalize_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _field_token(field_id: str, value: str) -> str:
    return f"{field_id}:{_sha256_bytes(_normalize_text(value).encode('utf-8'))}"


def _confirmation_token(field_id: str) -> str:
    """Bind confirmation recall to the frozen field, not OCR's guessed text."""

    return f"{field_id}:confirmation"


def _line_rect(line: ScreenshotSourceLineV1) -> tuple[float, float, float, float]:
    xs = [point[0] for point in line.bbox]
    ys = [point[1] for point in line.bbox]
    return min(xs), min(ys), max(xs), max(ys)


def _intersection_ratio(
    line_rect: tuple[float, float, float, float],
    region: tuple[int, int, int, int],
) -> float:
    left, top, right, bottom = line_rect
    region_left, region_top, region_right, region_bottom = region
    width = max(0.0, min(right, region_right) - max(left, region_left))
    height = max(0.0, min(bottom, region_bottom) - max(top, region_top))
    line_area = max(1.0, (right - left) * (bottom - top))
    return width * height / line_area


def _lines_for_field(
    field: G04FrozenFieldV1,
    lines: Sequence[ScreenshotSourceLineV1],
) -> tuple[ScreenshotSourceLineV1, ...]:
    inside: list[ScreenshotSourceLineV1] = []
    fallback: list[tuple[float, ScreenshotSourceLineV1]] = []
    region_left, region_top, region_right, region_bottom = field.region_xyxy
    region_center_x = (region_left + region_right) / 2
    region_center_y = (region_top + region_bottom) / 2
    for line in lines:
        rect = _line_rect(line)
        center_x = (rect[0] + rect[2]) / 2
        center_y = (rect[1] + rect[3]) / 2
        ratio = _intersection_ratio(rect, field.region_xyxy)
        if (
            rect[0] <= region_center_x <= rect[2]
            and rect[1] <= region_center_y <= rect[3]
        ) or (
            region_left <= center_x <= region_right
            and region_top <= center_y <= region_bottom
        ):
            inside.append(line)
        elif ratio >= 0.5:
            fallback.append((ratio, line))
    selected = inside or ([max(fallback, key=lambda item: item[0])[1]] if fallback else [])
    if len(selected) > 1:
        expected = _normalize_text(field.expected_text)
        text_matches = [
            line
            for line in selected
            if expected in _normalize_text(line.text)
        ]
        if text_matches:
            selected = text_matches
        else:
            def rank(line: ScreenshotSourceLineV1) -> tuple[float, float, int]:
                rect = _line_rect(line)
                distance = abs((rect[0] + rect[2]) / 2 - region_center_x) + abs(
                    (rect[1] + rect[3]) / 2 - region_center_y
                )
                return (
                    _intersection_ratio(rect, field.region_xyxy),
                    -distance,
                    -line.reading_index,
                )

            selected = [max(selected, key=rank)]
    return tuple(
        sorted(
            selected,
            key=lambda line: (_line_rect(line)[1], _line_rect(line)[0], line.reading_index),
        )
    )


def _extract_field_text(
    field: G04FrozenFieldV1,
    lines: Sequence[ScreenshotSourceLineV1],
) -> str | None:
    selected = _lines_for_field(field, lines)
    if not selected:
        return None
    joined = "".join(line.text.strip() for line in selected)
    if field.extraction == "WHOLE_LINE":
        return joined
    if field.extraction == "CONTAINS_EXPECTED_TEXT":
        expected = _normalize_text(field.expected_text)
        return field.expected_text if expected in _normalize_text(joined) else joined
    if "·" not in joined:
        return None
    before, after = joined.split("·", maxsplit=1)
    return before if field.extraction == "BEFORE_MIDDLE_DOT" else after


def _observed_field_position(
    field: G04FrozenFieldV1,
    selected: Sequence[ScreenshotSourceLineV1],
) -> tuple[int, int]:
    first_reading_index = min(line.reading_index for line in selected)
    joined = "".join(line.text.strip() for line in selected)
    expected = _normalize_text(field.expected_text)
    normalized_joined = _normalize_text(joined)
    occurrences: list[int] = []
    search_from = 0
    while True:
        substring_index = normalized_joined.find(expected, search_from)
        if substring_index < 0:
            break
        occurrences.append(substring_index)
        search_from = substring_index + max(1, len(expected))
    if occurrences:
        if len(occurrences) == 1:
            return first_reading_index, occurrences[0]
        first_line = min(selected, key=lambda line: line.reading_index)
        line_left, _line_top, line_right, _line_bottom = _line_rect(first_line)
        field_center_x = (field.region_xyxy[0] + field.region_xyxy[2]) / 2
        relative_x = min(
            1.0,
            max(0.0, (field_center_x - line_left) / max(1.0, line_right - line_left)),
        )
        estimated_character_center = relative_x * len(normalized_joined)
        chosen = min(
            occurrences,
            key=lambda index: abs(
                index + len(expected) / 2 - estimated_character_center
            ),
        )
        return first_reading_index, chosen
    # A tight independently adjudicated field rectangle is the deterministic
    # fallback when OCR changed the text so the expected substring is absent.
    return first_reading_index, field.region_xyxy[0]


def _observe_case(
    case: G04LicensedScreenshotCaseV1,
    lines: Sequence[ScreenshotSourceLineV1],
) -> FieldObservation:
    expected_tokens = tuple(_field_token(field.field_id, field.expected_text) for field in case.fields)
    expected_low = tuple(
        _confirmation_token(field.field_id)
        for field in case.fields
        if field.must_confirm
    )
    matched: list[tuple[int, int, int, str, bool]] = []
    observed_tokens: list[str] = []
    observed_confirmation: list[str] = []
    for field in case.fields:
        selected = _lines_for_field(field, lines)
        extracted = _extract_field_text(field, lines)
        if extracted is None or not selected:
            if field.must_confirm:
                raise FormalGateError("LOW_CONFIDENCE_FIELD_BINDING_MISMATCH")
            continue
        token = _field_token(field.field_id, extracted)
        observed_tokens.append(token)
        requires_confirmation = any(line.requires_confirmation for line in selected)
        if field.must_confirm != requires_confirmation:
            raise FormalGateError("LOW_CONFIDENCE_FIELD_BINDING_MISMATCH")
        if field.must_confirm and requires_confirmation:
            observed_confirmation.append(_confirmation_token(field.field_id))
        reading_index, subposition = _observed_field_position(field, selected)
        region_width = field.region_xyxy[2] - field.region_xyxy[0]
        matched.append(
            (
                reading_index,
                subposition,
                region_width,
                token,
                requires_confirmation,
            )
        )
    observed_order = tuple(item[3] for item in sorted(matched))
    return FieldObservation(
        expected_key_fields=expected_tokens,
        observed_key_fields=tuple(observed_tokens),
        expected_reading_order=expected_tokens,
        observed_reading_order=observed_order,
        expected_low_confidence_fields=expected_low,
        observed_confirmation_fields=tuple(observed_confirmation),
    )


def _observe_document(
    baseline: G04LicensedScreenshotBaselineV1,
    document: ScreenshotSourceDocumentV1,
) -> tuple[FieldObservation, ...]:
    return tuple(
        _observe_case(
            case,
            tuple(line for line in document.lines if line.image_index == image_index),
        )
        for image_index, case in enumerate(baseline.cases)
    )


async def _ocr_once(
    *,
    source_paths: Sequence[Path],
    cases: Sequence[Any],
    engine: ConcurrencyProbeOcrEngine,
    work_root: Path,
) -> tuple[ScreenshotSourceDocumentV1, float, int]:
    run_directory = Path(tempfile.mkdtemp(prefix="g04-paddle-", dir=work_root))
    staged_paths: list[Path] = []
    cleanup_count = 0
    try:
        secure_owner_only(run_directory, is_directory=True)
        assets: list[StagedScreenshotAsset] = []
        for source, case in zip(source_paths, cases, strict=True):
            staged = run_directory / f"{os.urandom(16).hex()}{source.suffix.lower()}"
            shutil.copyfile(source, staged)
            secure_owner_only(staged, is_directory=False)
            staged_paths.append(staged)
            assets.append(StagedScreenshotAsset(path=staged, content_hash=case.source_sha256))
        started = time.perf_counter_ns()
        document = await extract_screenshot_document(assets, engine, RUN_SPEC)
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        if document.partial or any(image.status != "SUCCEEDED" for image in document.images):
            raise FormalGateError("REAL_PADDLE_BATCH_NOT_COMPLETE")
        if duration_ms <= 0:
            raise FormalGateError("NON_POSITIVE_PERFORMANCE_SAMPLE")
        return document, duration_ms, len(staged_paths)
    finally:
        for path in staged_paths:
            try:
                path.unlink(missing_ok=True)
            finally:
                if not path.exists():
                    cleanup_count += 1
        shutil.rmtree(run_directory, ignore_errors=False)
        if run_directory.exists() or cleanup_count != len(staged_paths):
            raise FormalGateError("STAGED_SOURCE_CLEANUP_INCOMPLETE")


async def _run_ocr_suite(
    *,
    quality_source_paths: Sequence[Path],
    performance_source_paths: Sequence[Path],
    baseline: G04LicensedScreenshotBaselineV1,
    engine: ConcurrencyProbeOcrEngine,
    work_root: Path,
) -> OcrSuiteResult:
    quality_document, _quality_duration, cleanup_count = await _ocr_once(
        source_paths=quality_source_paths,
        cases=baseline.cases,
        engine=engine,
        work_root=work_root,
    )
    quality_observations = _observe_document(baseline, quality_document)

    for _ in range(2):
        _document, _duration, cleaned = await _ocr_once(
            source_paths=performance_source_paths,
            cases=baseline.synthetic_performance_cases,
            engine=engine,
            work_root=work_root,
        )
        cleanup_count += cleaned

    first_signature: str | None = None
    durations: list[float] = []
    for _ in range(20):
        document, duration, cleaned = await _ocr_once(
            source_paths=performance_source_paths,
            cases=baseline.synthetic_performance_cases,
            engine=engine,
            work_root=work_root,
        )
        cleanup_count += cleaned
        durations.append(duration)
        signature = document.document_hash
        if first_signature is None:
            first_signature = signature
        elif signature != first_signature:
            raise FormalGateError("MEASURED_PERFORMANCE_OCR_OUTPUT_NONDETERMINISTIC")
    if first_signature is None:
        raise FormalGateError("MEASURED_OCR_OUTPUT_MISSING")
    expected_cleanup_count = len(quality_source_paths) + (
        len(performance_source_paths) * (2 + 20)
    )
    if cleanup_count != expected_cleanup_count:
        raise FormalGateError("STAGED_SOURCE_CLEANUP_RECEIPT_COUNT_MISMATCH")
    return OcrSuiteResult(
        document=quality_document,
        observations=quality_observations,
        durations_ms=tuple(durations),
        staged_cleanup_count=cleanup_count,
        measured_output_signature=first_signature,
    )


def _image_projection(
    document: ScreenshotSourceDocumentV1,
    image_index: int,
) -> tuple[str, tuple[tuple[int, int], ...]]:
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for line in (item for item in document.lines if item.image_index == image_index):
        if parts:
            parts.append("\n")
            cursor += 1
        start = cursor
        parts.append(line.text)
        cursor += len(line.text)
        if line.requires_confirmation:
            spans.append((start, cursor))
    return "".join(parts), tuple(spans)


def _planned_activity_mentions(output: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                activity.compiled.mention.atomic_place_name
                for activity in output.activities
                if is_atomic_planned_place(activity.compiled.mention)
                and activity.compiled.mention.atomic_place_name
            }
        )
    )


def _serious_errors(
    *,
    case: G04LicensedScreenshotCaseV1,
    output: Any,
) -> tuple[G04SeriousErrorV1, ...]:
    errors: list[G04SeriousErrorV1] = []
    expected_names = {field.expected_text for field in case.fields if field.field_type == "ACTIVITY_PLACE"}
    expected_destinations = {field.expected_text for field in case.fields if field.field_type == "DESTINATION"}
    observed_destination = output.destination.get("name")
    if (
        (
            expected_destinations
            and observed_destination != "目的地待确认"
            and observed_destination not in expected_destinations
        )
        or (
            not expected_destinations
            and observed_destination != "目的地待确认"
        )
    ):
        errors.append(
            G04SeriousErrorV1(
                category="WRONG_CITY",
                item_id=_sha256_bytes(case.case_id.encode("utf-8")),
            )
        )
    forbidden = set(case.forbidden_place_predictions)
    for activity in output.activities:
        if (
            not is_atomic_planned_place(activity.compiled.mention)
            or activity.resolver_receipt.get("status")
            == "SOURCE_CONFIRMATION_REQUIRED"
        ):
            continue
        atomic_name = activity.compiled.mention.atomic_place_name
        if not atomic_name or atomic_name in expected_names:
            continue
        if atomic_name.startswith(("http://", "https://")):
            category = "URL_AS_PLACE"
        elif (
            atomic_name in forbidden
            or len(atomic_name) > 40
            or any(character in atomic_name for character in "。！？\n")
        ):
            category = "DESCRIPTION_AS_PLACE"
        else:
            category = "WRONG_CATEGORY"
        errors.append(
            G04SeriousErrorV1(
                category=category,
                item_id=_sha256_bytes(atomic_name.encode("utf-8")),
            )
        )
    deduplicated = {(error.category, error.item_id): error for error in errors}
    return tuple(deduplicated[key] for key in sorted(deduplicated))


_FORBIDDEN_PUBLIC_KEYS = {
    "batch_ref",
    "bbox",
    "confidence",
    "hash",
    "model",
    "offset",
    "provider",
    "receipt",
    "revision",
    "run_spec",
    "source_span",
    "span_end",
    "span_start",
    "uid",
}
_FORBIDDEN_PUBLIC_VALUE_FRAGMENTS = (
    "paddle",
    "provider",
    "runspec",
    "screen_shot_",
    "screenshot_",
    "source_confirmation_required",
)


def _public_internal_leak_count(value: Any) -> int:
    leaks = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_PUBLIC_KEYS:
                leaks += 1
            leaks += _public_internal_leak_count(item)
    elif isinstance(value, (list, tuple)):
        leaks += sum(_public_internal_leak_count(item) for item in value)
    elif isinstance(value, str):
        lowered = value.casefold()
        leaks += sum(fragment in lowered for fragment in _FORBIDDEN_PUBLIC_VALUE_FRAGMENTS)
    return leaks


async def _build_real_cases(
    *,
    baseline: G04LicensedScreenshotBaselineV1,
    suite: OcrSuiteResult,
) -> tuple[tuple[G04ScreenshotParityCaseV1, ...], int, int]:
    cases: list[G04ScreenshotParityCaseV1] = []
    low_confidence_poi_calls = 0
    guarded_place_count = 0
    internal_leak_count = 0
    for image_index, (case, observation) in enumerate(zip(baseline.cases, suite.observations, strict=True)):
        text_resolver = CountingPlaceResolver()
        text_pipeline = TripUnderstandingPipeline(
            inference_provider=DeterministicTextInferenceProvider(),
            place_resolver=text_resolver,
            max_place_concurrency=1,
        )
        text_output = await text_pipeline.run(case.paired_visible_text)

        screenshot_text, confirmation_spans = _image_projection(
            suite.document,
            image_index,
        )
        screenshot_resolver = CountingPlaceResolver()
        screenshot_pipeline = TripUnderstandingPipeline(
            inference_provider=DeterministicTextInferenceProvider(),
            place_resolver=screenshot_resolver,
            max_place_concurrency=1,
        )
        screenshot_output = await screenshot_pipeline.run(
            screenshot_text,
            requires_confirmation_spans=confirmation_spans,
        )
        internal_leak_count += _public_internal_leak_count(
            screenshot_output.public_result.model_dump(mode="json")
        )
        guarded_activities = [
            activity
            for activity in screenshot_output.activities
            if is_atomic_planned_place(activity.compiled.mention)
            and any(
                activity.compiled.mention.span_start < end
                and start < activity.compiled.mention.span_end
                for start, end in confirmation_spans
            )
        ]
        guarded_names = {
            activity.compiled.mention.atomic_place_name
            for activity in guarded_activities
            if activity.compiled.mention.atomic_place_name
        }
        guarded_place_count += len(guarded_activities)
        low_confidence_poi_calls += sum(
            1
            for _city, name in screenshot_resolver.calls
            if name in guarded_names
        )
        for guarded_activity in guarded_activities:
            if (
                guarded_activity.place is not None
                or guarded_activity.compiled.eligible_for_place_search
                or guarded_activity.resolver_receipt.get("status")
                != "SOURCE_CONFIRMATION_REQUIRED"
            ):
                raise FormalGateError("LOW_CONFIDENCE_PLACE_GUARD_NOT_APPLIED")
        controlled_names = {
            field.expected_text
            for field in case.fields
            if field.field_type == "ACTIVITY_PLACE" and field.must_confirm
        } | guarded_names
        serious = _serious_errors(
            case=case,
            output=screenshot_output,
        )
        oracle_items = tuple(
            G04PlaceOracleItemV1(
                oracle_id=field.field_id,
                expected_text=field.expected_text,
                activity_role=field.activity_role,
                metric_eligibility=field.place_metric_eligibility,
            )
            for field in case.fields
            if field.field_type == "ACTIVITY_PLACE"
        )
        text_mentions = tuple(name for name in _planned_activity_mentions(text_output) if name not in controlled_names)
        screenshot_mentions = tuple(
            name for name in _planned_activity_mentions(screenshot_output) if name not in controlled_names
        )
        if "REAL_PLANNED_PARITY" in case.metric_scope:
            if not text_mentions:
                raise FormalGateError("TEXT_PIPELINE_PLANNED_ACTIVITY_NOT_AVAILABLE")
            if not screenshot_mentions:
                raise FormalGateError("SCREENSHOT_PIPELINE_PLANNED_ACTIVITY_NOT_AVAILABLE")
            place_metric = G04EvaluatedPlaceMetricV1(
                status="EVALUATED",
                oracle_items=oracle_items,
                reference_executable_places=case.parity_places,
                text_executable_places=text_mentions,
                screenshot_executable_places=screenshot_mentions,
            )
        else:
            if "REAL_LOW_CONFIDENCE_CONTROL" in case.metric_scope:
                reason = "LOW_CONFIDENCE_CONTROL"
            elif "REFERENCE_CONTROL" in case.metric_scope:
                reason = "REFERENCE_CONTROL_ROLE"
            else:
                reason = "OCR_ONLY_SCOPE"
            place_metric = G04NotApplicablePlaceMetricV1(
                status="NOT_APPLICABLE",
                reason=reason,
                oracle_items=oracle_items,
                reference_executable_places="NOT_APPLICABLE",
                text_executable_places="NOT_APPLICABLE",
                screenshot_executable_places="NOT_APPLICABLE",
            )
        cases.append(
            G04ScreenshotParityCaseV1(
                case_id=case.case_id,
                source=G04SourceEvidenceV1(
                    evidence_tier=case.evidence_tier,
                    source_sha256=case.source_sha256,
                    license_name=case.license.license_name,
                    license_reference=str(case.license.source_url),
                ),
                metric_scope=case.metric_scope,
                expected_key_fields=observation.expected_key_fields,
                observed_key_fields=observation.observed_key_fields,
                expected_reading_order=observation.expected_reading_order,
                observed_reading_order=observation.observed_reading_order,
                expected_low_confidence_fields=(observation.expected_low_confidence_fields),
                observed_confirmation_fields=observation.observed_confirmation_fields,
                place_metric=place_metric,
                serious_errors=serious,
            )
        )
    if guarded_place_count == 0:
        raise FormalGateError("LOW_CONFIDENCE_PLACE_GUARD_NOT_EXERCISED")
    return tuple(cases), low_confidence_poi_calls, internal_leak_count


def _synthetic_manifest_cases(
    baseline: G04LicensedScreenshotBaselineV1,
) -> tuple[G04ScreenshotParityCaseV1, ...]:
    spec_hash = baseline.synthetic_spec_sha256
    return tuple(
        G04ScreenshotParityCaseV1(
            case_id=item.case_id,
            source=G04SourceEvidenceV1(
                evidence_tier="SYNTHETIC_FORMAT_ONLY",
                source_sha256=canonical_sha256(item.model_dump(mode="json")),
                synthetic_spec_sha256=spec_hash,
            ),
            metric_scope=("SYNTHETIC_FORMAT_CONTROL",),
            expected_key_fields=(f"format:{item.case_id}",),
            observed_key_fields=(),
            expected_reading_order=(f"format:{item.case_id}",),
            observed_reading_order=(),
            expected_low_confidence_fields=(),
            observed_confirmation_fields=(),
            place_metric=G04NotApplicablePlaceMetricV1(
                status="NOT_APPLICABLE",
                reason="SYNTHETIC_FORMAT_ONLY",
                oracle_items=(),
                reference_executable_places="NOT_APPLICABLE",
                text_executable_places="NOT_APPLICABLE",
                screenshot_executable_places="NOT_APPLICABLE",
            ),
            serious_errors=(),
        )
        for item in baseline.synthetic_format_cases
    )


def _binding_hashes() -> dict[str, str]:
    paths = {
        "runner_sha256": Path(__file__).resolve(),
        "contracts_sha256": BACKEND_ROOT / "evals" / "g04_screenshot" / "contracts.py",
        "scorer_sha256": BACKEND_ROOT / "evals" / "g04_screenshot" / "scorer.py",
        "baseline_contract_sha256": (BACKEND_ROOT / "evals" / "g04_screenshot" / "baseline.py"),
    }
    return {name: _sha256_file(path) for name, path in paths.items()}


def _formal_projection(
    *,
    candidate_commit: str,
    baseline: G04LicensedScreenshotBaselineV1,
    runtime_binding: dict[str, Any],
    model_bindings: dict[str, str],
    device: str,
    manifest: G04ScreenshotParityManifestV1,
    score: Any,
    suite: OcrSuiteResult,
    source_cleanup_receipt: str,
    internal_leak_count: int,
    failures: Sequence[str],
) -> dict[str, Any]:
    bindings = _binding_hashes()
    adjudicator = next(item for item in baseline.review.bindings if item.role.endswith("INDEPENDENT_ADJUDICATOR"))
    expected_transcript_sha256 = baseline.review.oracle_projection_sha256
    paired_visible_text_dataset_sha256 = canonical_sha256(
        [
            {
                "case_id": case.case_id,
                "paired_visible_text_sha256": (
                    case.capture_provenance.paired_visible_text_sha256
                    if case.capture_provenance is not None
                    else "NOT_APPLICABLE"
                ),
            }
            for case in baseline.cases
        ]
    )
    license_manifest_sha256 = canonical_sha256(
        [
            {
                "case_id": case.case_id,
                "evidence_tier": case.evidence_tier,
                "source_sha256": case.source_sha256,
                "license": case.license.model_dump(mode="json"),
                "capture_provenance": (
                    case.capture_provenance.model_dump(
                        mode="json",
                        exclude={"paired_visible_text"},
                    )
                    if case.capture_provenance is not None
                    else None
                ),
            }
            for case in baseline.cases
        ]
    )
    model_sha256 = canonical_sha256(model_bindings)
    config_sha256 = canonical_sha256(
        {
            "device": device,
            "detection_model": model_bindings["detection_model"],
            "recognition_model": model_bindings["recognition_model"],
            "enable_mkldnn": False,
            "lang": None,
            "run_spec": RUN_SPEC.model_dump(mode="json"),
        }
    )
    hardware_sha256 = canonical_sha256(runtime_binding)
    driver_sha256 = canonical_sha256(
        {
            "gpu_driver_version": runtime_binding["gpu_driver_version"],
            "compiled_cuda": runtime_binding["paddle_compiled_cuda_version"],
            "compiled_cudnn": runtime_binding["paddle_compiled_cudnn_version"],
            "loaded_cudnn": runtime_binding["loaded_cudnn_runtime_version"],
        }
    )
    measurements = [round(float(value), 3) for value in suite.durations_ms]
    p95_ms = sorted(measurements)[18]
    serious_errors = [error for case in manifest.cases for error in case.serious_errors]
    gate_pass = not failures
    source_cleanup_count = (
        len(baseline.cases) + len(baseline.synthetic_performance_cases)
        if gate_pass
        else 0
    )
    cleanup_count = suite.staged_cleanup_count + source_cleanup_count
    cleanup_receipts_sha256 = canonical_sha256(
        {
            "staged_cleanup_receipt_count": suite.staged_cleanup_count,
            "source_cleanup_receipt_sha256": source_cleanup_receipt,
            "source_cleanup_receipt_count": source_cleanup_count,
        }
    )
    formal: dict[str, Any] = {
        "schema_version": "g04-screenshot-parity-receipt-v1",
        "goal_id": "TC-VNEXT-G04-SCREENSHOT",
        "evidence_level": "REAL_PADDLE_LICENSED_SCREENSHOT_PARITY",
        "execution_mode": "REAL_PADDLE_LOCAL",
        "sanitized": True,
        "candidate": {
            "commit": candidate_commit,
            "tree": _git("rev-parse", f"{candidate_commit}^{{tree}}"),
            "product_fingerprint": _cross_platform_product_fingerprint(candidate_commit),
        },
        "evaluator": {
            "baseline_manifest_sha256": _sha256_file(DEFAULT_BASELINE),
            "expected_transcript_sha256": expected_transcript_sha256,
            "runspec_sha256": canonical_sha256(RUN_SPEC.model_dump(mode="json")),
            "runner_sha256": bindings["runner_sha256"],
            "scorer_sha256": bindings["scorer_sha256"],
            # The current governance receipt key is retained for schema
            # compatibility; its bound value is the exact paired visible-text
            # dataset, never a derived text-day baseline.
            "text_only_day_dataset_sha256": paired_visible_text_dataset_sha256,
            "oracle_adjudication_sha256": adjudicator.output_sha256,
            "metric_inputs_sha256": canonical_sha256(
                {
                    "quality_manifest": manifest.model_dump(mode="json"),
                    "quality_source_hashes": [case.source_sha256 for case in baseline.cases],
                    "performance_source_hashes": [case.source_sha256 for case in baseline.synthetic_performance_cases],
                }
            ),
            "scored_outputs_sha256": canonical_sha256(score.model_dump(mode="json")),
        },
        "source_policy": {
            "originals_in_git": False,
            "originals_storage": "OUTSIDE_GIT_EPHEMERAL",
            "license_manifest_sha256": license_manifest_sha256,
            "cleanup_receipts_sha256": cleanup_receipts_sha256,
            "terminal_cleanup_receipts_complete": gate_pass,
            "parity_metric_scope": "LICENSED_REAL_ONLY",
            "review_label": "MULTI_AGENT_SIMULATED_REVIEW",
            "human_review": False,
            "candidate_outputs_used_for_oracle": False,
        },
        "paddle": {
            "paddleocr_version": runtime_binding["paddleocr_version"],
            "paddlepaddle_version": runtime_binding["paddlepaddle_version"],
            "model_sha256": model_sha256,
            "config_sha256": config_sha256,
        },
        "hardware": {
            "device_class": "LOCAL_GPU",
            "hardware_sha256": hardware_sha256,
            "driver_sha256": driver_sha256,
        },
        "performance": {
            "image_count": 3,
            "width_px": 1080,
            "height_px": 1920,
            "concurrency": 1,
            "warmup_runs": 2,
            "measured_runs": 20,
            "measurement_ms": measurements,
            "p95_ms": p95_ms,
        },
        "metric_counts": {
            "case_count": score.licensed_real_case_count + score.synthetic_format_case_count,
            "licensed_real_case_count": score.licensed_real_case_count,
            "synthetic_case_count": score.synthetic_format_case_count,
            "text_only_day_case_count": sum(
                "REAL_PLANNED_PARITY" in case.metric_scope
                for case in baseline.cases
            ),
            "critical_field_count": score.key_field_expected_count,
            "low_confidence_critical_field_count": (score.low_confidence_expected_count),
            "reading_adjacency_count": score.reading_adjacency_expected_count,
            "location_baseline_count": score.reference_place_count,
            "cleanup_terminal_count": cleanup_count,
            "cleanup_receipt_count": cleanup_count,
        },
        "metrics": {
            "critical_field_f1": score.key_field_f1,
            "low_confidence_confirmation_recall": (score.low_confidence_confirmation_recall),
            "reading_order_adjacency_f1": score.adjacency_f1,
            "location_precision_drop_pp": score.place_precision_drop_pp,
            "location_recall_drop_pp": score.place_recall_drop_pp,
            "wrong_city_count": sum(error.category == "WRONG_CITY" for error in serious_errors),
            "wrong_category_count": sum(error.category == "WRONG_CATEGORY" for error in serious_errors),
            "sentence_as_place_count": sum(
                error.category in {"DESCRIPTION_AS_PLACE", "URL_AS_PLACE"} for error in serious_errors
            ),
            "internal_leak_count": internal_leak_count,
            "cleanup_receipt_coverage": 1.0 if gate_pass else 0.0,
        },
        "decision": {
            "status": "PASS" if gate_pass else "FAIL",
            "failures": sorted(set(failures)),
        },
    }
    formal["receipt_hash"] = canonical_receipt_hash(formal)
    return formal


def _external_execution_receipt(
    *,
    formal_projection: dict[str, Any],
    runtime_binding: dict[str, Any],
    suite: OcrSuiteResult,
    low_confidence_poi_calls: int,
    score: Any,
    baseline: G04LicensedScreenshotBaselineV1,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "g04-paddle-external-execution-receipt-v1",
        "status": formal_projection["decision"]["status"],
        "formal_projection": formal_projection,
        "diagnostics": {
            "runtime_binding": runtime_binding,
            "measured_output_signature_sha256": suite.measured_output_signature,
            "actual_max_ocr_concurrency": formal_projection["performance"]["concurrency"],
            "low_confidence_poi_call_count": low_confidence_poi_calls,
            "metric_scope_counts": {
                "licensed_real_ocr_case_count": (score.licensed_real_ocr_case_count),
                "licensed_real_planned_parity_case_count": (score.licensed_real_planned_parity_case_count),
                "licensed_real_low_confidence_control_count": sum(
                    "REAL_LOW_CONFIDENCE_CONTROL" in case.metric_scope
                    for case in baseline.cases
                ),
                "reference_control_case_count": score.reference_control_case_count,
            },
            "source_bindings": {
                "quality_source_hashes": [case.source_sha256 for case in baseline.cases],
                "performance_source_hashes": [case.source_sha256 for case in baseline.synthetic_performance_cases],
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
    payload["external_receipt_hash"] = canonical_sha256(payload)
    return payload


def sanitize_external_receipt(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) != {
        "schema_version",
        "status",
        "formal_projection",
        "diagnostics",
        "external_receipt_hash",
    }:
        raise FormalGateError("EXTERNAL_RECEIPT_FIELDS_INVALID")
    if raw["schema_version"] != "g04-paddle-external-execution-receipt-v1":
        raise FormalGateError("EXTERNAL_RECEIPT_SCHEMA_INVALID")
    provided_hash = raw["external_receipt_hash"]
    canonical = canonical_sha256({key: value for key, value in raw.items() if key != "external_receipt_hash"})
    if provided_hash != canonical:
        raise FormalGateError("EXTERNAL_RECEIPT_HASH_INVALID")
    if raw["status"] != "PASS":
        raise FormalGateError("EXTERNAL_RECEIPT_NOT_PASS")
    diagnostics = raw["diagnostics"]
    if not isinstance(diagnostics, dict):
        raise FormalGateError("EXTERNAL_DIAGNOSTICS_INVALID")
    required_diagnostics = {
        "runtime_binding",
        "measured_output_signature_sha256",
        "actual_max_ocr_concurrency",
        "low_confidence_poi_call_count",
        "metric_scope_counts",
        "source_bindings",
        "planned_parity_basis",
        "deep_city_provider_evidence",
        "qwen_vl_status",
        "human_usability",
        "production",
        "public_internet_e2e",
        "commercial",
    }
    if set(diagnostics) != required_diagnostics:
        raise FormalGateError("EXTERNAL_DIAGNOSTICS_FIELDS_INVALID")
    scope_counts = diagnostics["metric_scope_counts"]
    if (
        not isinstance(scope_counts, dict)
        or scope_counts.get("licensed_real_ocr_case_count") != 3
        or scope_counts.get("licensed_real_planned_parity_case_count") != 3
        or scope_counts.get("licensed_real_low_confidence_control_count") != 1
        or not isinstance(scope_counts.get("reference_control_case_count"), int)
        or scope_counts["reference_control_case_count"] < 0
    ):
        raise FormalGateError("EXTERNAL_METRIC_SCOPE_COUNTS_INVALID")
    source_bindings = diagnostics["source_bindings"]
    if not isinstance(source_bindings, dict) or set(source_bindings) != {
        "quality_source_hashes",
        "performance_source_hashes",
        "synthetic_performance_quality_eligibility",
    }:
        raise FormalGateError("EXTERNAL_SOURCE_BINDINGS_INVALID")
    quality_hashes = source_bindings["quality_source_hashes"]
    performance_hashes = source_bindings["performance_source_hashes"]
    if (
        not isinstance(quality_hashes, list)
        or not isinstance(performance_hashes, list)
        or len(quality_hashes) != 4
        or len(performance_hashes) != 3
        or len(set(quality_hashes)) != 4
        or len(set(performance_hashes)) != 3
        or set(quality_hashes) & set(performance_hashes)
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in (*quality_hashes, *performance_hashes)
        )
        or source_bindings["synthetic_performance_quality_eligibility"] != "NOT_APPLICABLE"
    ):
        raise FormalGateError("EXTERNAL_SOURCE_HASH_SPLIT_INVALID")
    if (
        diagnostics["planned_parity_basis"] != "SAME_TRIP_UNDERSTANDING_PIPELINE_ACTIVITY_MENTIONS"
        or diagnostics["deep_city_provider_evidence"] != "NOT_APPLICABLE_NON_DEEP_CITY_SOURCE_SET"
        or diagnostics["qwen_vl_status"] != "NOT_RUN_NO_EXACT_BINDING"
    ):
        raise FormalGateError("EXTERNAL_EVIDENCE_BOUNDARY_INVALID")
    formal = raw["formal_projection"]
    if not isinstance(formal, dict):
        raise FormalGateError("FORMAL_PROJECTION_INVALID")
    try:
        _validate_formal_receipt(formal)
    except G04ParityReceiptError as exc:
        raise FormalGateError("FORMAL_PROJECTION_REJECTED") from exc
    return formal


def _sha256_at_commit(candidate_commit: str, relative: str) -> str:
    process = subprocess.run(
        ["git", "show", f"{candidate_commit}:{relative}"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if process.returncode != 0:
        raise FormalGateError("CANDIDATE_EVALUATOR_BLOB_MISSING")
    return _sha256_bytes(process.stdout)


def _validate_projection_candidate_bindings(
    formal: dict[str, Any],
    diagnostics: dict[str, Any],
) -> None:
    candidate = formal["candidate"]
    commit = candidate["commit"]
    if _git("rev-parse", f"{commit}^{{commit}}") != commit:
        raise FormalGateError("FORMAL_CANDIDATE_COMMIT_UNAVAILABLE")
    if _git("rev-parse", f"{commit}^{{tree}}") != candidate["tree"]:
        raise FormalGateError("FORMAL_CANDIDATE_TREE_MISMATCH")
    if _cross_platform_product_fingerprint(commit) != candidate["product_fingerprint"]:
        raise FormalGateError("FORMAL_PRODUCT_FINGERPRINT_MISMATCH")
    evaluator = formal["evaluator"]
    for field, relative in {
        "baseline_manifest_sha256": ("backend/eval_data/g04_screenshot/licensed_baseline_v1.json"),
        "runner_sha256": "backend/scripts/run_g04_paddle_gate.py",
        "scorer_sha256": "backend/evals/g04_screenshot/scorer.py",
    }.items():
        if _sha256_at_commit(commit, relative) != evaluator[field]:
            raise FormalGateError("FORMAL_EVALUATOR_BLOB_MISMATCH")
    baseline_process = subprocess.run(
        [
            "git",
            "show",
            (f"{commit}:backend/eval_data/g04_screenshot/licensed_baseline_v1.json"),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if baseline_process.returncode != 0:
        raise FormalGateError("FORMAL_CANDIDATE_BASELINE_MISSING")
    try:
        candidate_baseline = G04LicensedScreenshotBaselineV1.model_validate(
            json.loads(baseline_process.stdout.decode("utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FormalGateError("FORMAL_CANDIDATE_BASELINE_INVALID") from exc
    oracle_relative = candidate_baseline.review.adjudication_artifact_path
    if (
        _sha256_at_commit(commit, oracle_relative)
        != candidate_baseline.review.adjudication_artifact_sha256
    ):
        raise FormalGateError("FORMAL_ORACLE_ARTIFACT_BLOB_MISMATCH")
    oracle_process = subprocess.run(
        ["git", "show", f"{commit}:{oracle_relative}"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    try:
        candidate_oracle = json.loads(oracle_process.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormalGateError("FORMAL_ORACLE_ARTIFACT_INVALID") from exc
    if (
        oracle_process.returncode != 0
        or candidate_oracle.get("oracle_projection")
        != oracle_projection_payload(candidate_baseline.cases)
        or canonical_sha256(candidate_oracle.get("oracle_projection"))
        != candidate_baseline.review.oracle_projection_sha256
    ):
        raise FormalGateError("FORMAL_ORACLE_PROJECTION_MISMATCH")
    source_bindings = diagnostics["source_bindings"]
    if source_bindings["quality_source_hashes"] != [case.source_sha256 for case in candidate_baseline.cases]:
        raise FormalGateError("FORMAL_QUALITY_SOURCE_BINDING_MISMATCH")
    if source_bindings["performance_source_hashes"] != [
        case.source_sha256 for case in candidate_baseline.synthetic_performance_cases
    ]:
        raise FormalGateError("FORMAL_PERFORMANCE_SOURCE_BINDING_MISMATCH")
    for case in candidate_baseline.synthetic_performance_cases:
        if _sha256_at_commit(commit, case.render_script_repository_path) != case.render_script_sha256:
            raise FormalGateError("FORMAL_PERFORMANCE_RENDERER_BINDING_MISMATCH")


def _delete_frozen_sources(
    source_paths: Sequence[Path],
    source_root: Path,
) -> str:
    hashes = [_sha256_file(path) for path in source_paths]
    for path in source_paths:
        path.unlink()
    if any(path.exists() for path in source_paths) or any(source_root.iterdir()):
        raise FormalGateError("FROZEN_SOURCE_TERMINAL_CLEANUP_INCOMPLETE")
    return canonical_sha256(
        {
            "action": "DELETE_FROZEN_SOURCE_AFTER_FORMAL_GATE",
            "deleted_source_sha256": sorted(hashes),
            "deleted_count": len(hashes),
        }
    )


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
    temporary.write_text(f"{encoded}\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _preflight_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    probe = path.parent / f".g04-output-probe-{os.urandom(8).hex()}"
    try:
        with probe.open("xb") as stream:
            stream.write(b"g04-output-preflight")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise FormalGateError("OUTPUT_NOT_DURABLY_WRITABLE") from exc
    finally:
        probe.unlink(missing_ok=True)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    candidate_commit = _validate_clean_candidate(args.candidate_commit)
    baseline_path = args.baseline.resolve()
    if baseline_path != DEFAULT_BASELINE.resolve():
        raise FormalGateError("BASELINE_PATH_NOT_FROZEN_DEFAULT")
    baseline = _load_baseline(baseline_path)
    _require_formal_evaluable_baseline(baseline)

    quality_source_root = _require_external_path(
        args.quality_source_root,
        code="QUALITY_SOURCE_ROOT_INSIDE_REPOSITORY",
    )
    performance_source_root = _require_external_path(
        args.performance_source_root,
        code="PERFORMANCE_SOURCE_ROOT_INSIDE_REPOSITORY",
    )
    work_root = _require_external_path(args.work_root, code="WORK_ROOT_INSIDE_REPOSITORY")
    output_path = _require_external_path(args.output, code="OUTPUT_INSIDE_REPOSITORY")
    if _inside(quality_source_root, performance_source_root) or _inside(performance_source_root, quality_source_root):
        raise FormalGateError("QUALITY_AND_PERFORMANCE_ROOTS_OVERLAP")
    if (
        _inside(quality_source_root, work_root)
        or _inside(work_root, quality_source_root)
        or _inside(performance_source_root, work_root)
        or _inside(work_root, performance_source_root)
    ):
        raise FormalGateError("SOURCE_AND_WORK_ROOTS_OVERLAP")
    if _inside(quality_source_root, output_path) or _inside(
        performance_source_root,
        output_path,
    ):
        raise FormalGateError("OUTPUT_INSIDE_SOURCE_ROOT")
    _preflight_output(output_path)
    if not args.delete_source_on_success:
        raise FormalGateError("SOURCE_TERMINAL_CLEANUP_NOT_ENABLED")
    work_root.mkdir(parents=True, exist_ok=True)
    if work_root.is_symlink() or any(work_root.iterdir()):
        raise FormalGateError("WORK_ROOT_NOT_EMPTY")
    secure_owner_only(work_root, is_directory=True)
    quality_source_paths = _validate_source_images(
        quality_source_root,
        baseline.cases,
    )
    performance_source_paths = _validate_source_images(
        performance_source_root,
        baseline.synthetic_performance_cases,
    )

    detection_model_dir = _require_external_path(
        args.detection_model_dir,
        code="DETECTION_MODEL_INSIDE_REPOSITORY",
    )
    recognition_model_dir = _require_external_path(
        args.recognition_model_dir,
        code="RECOGNITION_MODEL_INSIDE_REPOSITORY",
    )
    if detection_model_dir.name != "PP-OCRv5_mobile_det":
        raise FormalGateError("DETECTION_MODEL_NAME_MISMATCH")
    if recognition_model_dir.name != "PP-OCRv5_mobile_rec":
        raise FormalGateError("RECOGNITION_MODEL_NAME_MISMATCH")
    model_bindings = {
        "detection_model": detection_model_dir.name,
        "detection_model_tree_sha256": _tree_sha256(detection_model_dir),
        "recognition_model": recognition_model_dir.name,
        "recognition_model_tree_sha256": _tree_sha256(recognition_model_dir),
    }
    runtime_binding = _runtime_binding()
    options: dict[str, Any] = {
        "lang": None,
        "text_detection_model_name": detection_model_dir.name,
        "text_detection_model_dir": str(detection_model_dir),
        "text_recognition_model_name": recognition_model_dir.name,
        "text_recognition_model_dir": str(recognition_model_dir),
        "enable_mkldnn": False,
    }
    if args.device.strip():
        options["device"] = args.device.strip()
    adapter = PaddleOcrAdapter(options=options)
    engine = ConcurrencyProbeOcrEngine(adapter)
    try:
        suite = await _run_ocr_suite(
            quality_source_paths=quality_source_paths,
            performance_source_paths=performance_source_paths,
            baseline=baseline,
            engine=engine,
            work_root=work_root,
        )
    finally:
        engine.close()
    if engine.max_active != 1:
        raise FormalGateError("REAL_OCR_MAX_CONCURRENCY_NOT_ONE")

    real_cases, low_confidence_poi_calls, internal_leak_count = await _build_real_cases(
        baseline=baseline,
        suite=suite,
    )
    manifest = G04ScreenshotParityManifestV1(
        schema_version="g04-screenshot-parity-manifest-v1",
        cases=(*real_cases, *_synthetic_manifest_cases(baseline)),
        performance=G04PerformanceEvidenceV1(
            warmup_runs=2,
            measured_runs=20,
            image_count=3,
            image_width=1080,
            image_height=1920,
            max_concurrency=engine.max_active,
            durations_ms=suite.durations_ms,
        ),
    )
    try:
        score = score_g04_screenshot_manifest(manifest)
    except G04ScreenshotManifestError as exc:
        raise FormalGateError("FORMAL_MANIFEST_REJECTED") from exc
    extra_failures: list[str] = []
    if low_confidence_poi_calls != 0:
        extra_failures.append("LOW_CONFIDENCE_PLACE_TRIGGERED_POI_CALL")
    if internal_leak_count != 0:
        extra_failures.append("PUBLIC_RESULT_INTERNAL_FIELD_LEAK_NONZERO")
    expected_staged_cleanup_count = len(quality_source_paths) + (
        len(performance_source_paths) * (2 + 20)
    )
    if suite.staged_cleanup_count != expected_staged_cleanup_count:
        extra_failures.append("STAGED_SOURCE_CLEANUP_NOT_100_PERCENT")
    if not score.gate_pass:
        extra_failures.extend(score.failures)
    gate_pass = not extra_failures
    if not gate_pass:
        source_cleanup_receipt = "NOT_RUN_GATE_FAILED"
    else:
        source_cleanup_receipt = canonical_sha256(
            {
                "quality": _delete_frozen_sources(
                    quality_source_paths,
                    quality_source_root,
                ),
                "performance": _delete_frozen_sources(
                    performance_source_paths,
                    performance_source_root,
                ),
            }
        )

    formal = _formal_projection(
        candidate_commit=candidate_commit,
        baseline=baseline,
        runtime_binding=runtime_binding,
        model_bindings=model_bindings,
        device=args.device.strip(),
        manifest=manifest,
        score=score,
        suite=suite,
        source_cleanup_receipt=source_cleanup_receipt,
        internal_leak_count=internal_leak_count,
        failures=extra_failures,
    )
    return _external_execution_receipt(
        formal_projection=formal,
        runtime_binding=runtime_binding,
        suite=suite,
        low_confidence_poi_calls=low_confidence_poi_calls,
        score=score,
        baseline=baseline,
    )


def _error_payload(code: str) -> dict[str, Any]:
    return {
        "schema_version": "g04-paddle-formal-gate-error-v1",
        "status": (
            "NOT_EVALUABLE"
            if code
            in {
                "DERIVED_TEXT_SCREENSHOT_NOT_EVALUABLE",
                "LICENSED_REAL_SCREENSHOT_BASELINE_NOT_FROZEN",
                "PENDING_CAPTURE",
                "PENDING_FRESH_HASH_BOUND_REVIEW",
                "SCREENSHOT_PIPELINE_PLANNED_ACTIVITY_NOT_AVAILABLE",
                "TEXT_PIPELINE_PLANNED_ACTIVITY_NOT_AVAILABLE",
            }
            else "INVALID_OR_INCOMPLETE"
        ),
        "error_code": code,
        "raw_source_text_in_receipt": False,
        "source_paths_in_receipt": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or sanitize the fail-closed G04 Paddle screenshot parity gate")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run",
        help="write a detailed execution receipt outside Git",
    )
    run.add_argument("--quality-source-root", type=Path, required=True)
    run.add_argument("--performance-source-root", type=Path, required=True)
    run.add_argument("--work-root", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--candidate-commit", required=True)
    run.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    run.add_argument("--detection-model-dir", type=Path, required=True)
    run.add_argument("--recognition-model-dir", type=Path, required=True)
    run.add_argument("--device", default="gpu:0")
    run.add_argument("--delete-source-on-success", action="store_true")

    sanitize = commands.add_parser(
        "sanitize",
        help="project a passing external receipt into the exact Git receipt schema",
    )
    sanitize.add_argument("--external-receipt", type=Path, required=True)
    sanitize.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / FORMAL_RECEIPT_PATH,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sanitize":
        try:
            external_path = _require_external_path(
                args.external_receipt,
                code="EXTERNAL_RECEIPT_INSIDE_REPOSITORY",
            )
            output_path = args.output.resolve()
            if output_path != (REPOSITORY_ROOT / FORMAL_RECEIPT_PATH).resolve():
                raise FormalGateError("FORMAL_GIT_RECEIPT_PATH_INVALID")
            raw = json.loads(external_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise FormalGateError("EXTERNAL_RECEIPT_ROOT_INVALID")
            formal = sanitize_external_receipt(raw)
            _validate_projection_candidate_bindings(
                formal,
                raw["diagnostics"],
            )
            _write_receipt(output_path, formal)
            print(json.dumps(formal, ensure_ascii=False, sort_keys=True))
            return 0
        except (OSError, json.JSONDecodeError, FormalGateError) as exc:
            code = exc.code if isinstance(exc, FormalGateError) else "SANITIZE_IO_INVALID"
            print(json.dumps(_error_payload(code), sort_keys=True), file=sys.stderr)
            return 2

    output_path: Path | None = None
    try:
        output_path = _require_external_path(args.output, code="OUTPUT_INSIDE_REPOSITORY")
        payload = asyncio.run(_run(args))
        _write_receipt(output_path, payload)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0 if payload["status"] == "PASS" else 1
    except FormalGateError as exc:
        payload = _error_payload(exc.code)
    except Exception:
        payload = _error_payload("UNEXPECTED_FORMAL_GATE_FAILURE")
    if output_path is not None:
        try:
            _write_receipt(output_path, payload)
        except OSError:
            pass
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
