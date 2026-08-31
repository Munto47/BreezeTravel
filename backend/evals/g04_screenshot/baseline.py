from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .contracts import G04ActivityRole, G04MetricScope


HASH_PATTERN = r"^[0-9a-f]{64}$"
PENDING_REVIEW_HASH = "PENDING_FRESH_HASH_BOUND_REVIEW"
PENDING_CAPTURE_HASH = "PENDING_CAPTURE"


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class G04ReviewBindingV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(
        pattern=r"^/root/g04_oracle_(?:transcriber_a|transcriber_b|adjudicator)$"
    )
    role: Literal[
        "G04_ORACLE_INDEPENDENT_VISUAL_TRANSCRIBER_A",
        "G04_ORACLE_INDEPENDENT_VISUAL_TRANSCRIBER_B",
        "G04_ORACLE_INDEPENDENT_ADJUDICATOR",
    ]
    task_id_sha256: str = Field(pattern=HASH_PATTERN)
    prompt_sha256: str = Field(pattern=rf"^(?:[0-9a-f]{{64}}|{PENDING_REVIEW_HASH})$")
    output_sha256: str = Field(pattern=rf"^(?:[0-9a-f]{{64}}|{PENDING_REVIEW_HASH})$")

    @model_validator(mode="after")
    def validate_task_binding(self) -> "G04ReviewBindingV1":
        expected = hashlib.sha256(self.task_id.encode("utf-8")).hexdigest()
        if self.task_id_sha256 != expected:
            raise ValueError("review task ID hash does not match the exact task ID")
        return self

    @property
    def is_frozen(self) -> bool:
        return self.prompt_sha256 != PENDING_REVIEW_HASH and self.output_sha256 != PENDING_REVIEW_HASH


class G04ReviewEvidenceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_level: Literal["MULTI_AGENT_SIMULATED_REVIEW"]
    status: Literal["PENDING_FRESH_HASH_BOUND_REVIEW", "FROZEN_HASH_BOUND"]
    isolated_transcriber_count: Literal[2]
    adjudicator_count: Literal[1]
    human_evidence: Literal[False]
    bindings: tuple[G04ReviewBindingV1, G04ReviewBindingV1, G04ReviewBindingV1]
    adjudication_artifact_path: Literal[
        "backend/eval_data/g04_screenshot/adjudicated_oracle_v1.json"
    ]
    adjudication_artifact_sha256: str = Field(
        pattern=rf"^(?:[0-9a-f]{{64}}|{PENDING_REVIEW_HASH})$"
    )
    oracle_projection_sha256: str = Field(
        pattern=rf"^(?:[0-9a-f]{{64}}|{PENDING_REVIEW_HASH})$"
    )
    notes: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_panel(self) -> "G04ReviewEvidenceV1":
        expected_roles = {
            "G04_ORACLE_INDEPENDENT_VISUAL_TRANSCRIBER_A",
            "G04_ORACLE_INDEPENDENT_VISUAL_TRANSCRIBER_B",
            "G04_ORACLE_INDEPENDENT_ADJUDICATOR",
        }
        observed_roles = {item.role for item in self.bindings}
        task_ids = {item.task_id for item in self.bindings}
        if observed_roles != expected_roles or len(task_ids) != 3:
            raise ValueError("review bindings must contain distinct A/B/adjudicator tasks")
        all_frozen = (
            all(item.is_frozen for item in self.bindings)
            and self.adjudication_artifact_sha256 != PENDING_REVIEW_HASH
            and self.oracle_projection_sha256 != PENDING_REVIEW_HASH
        )
        if (self.status == "FROZEN_HASH_BOUND") != all_frozen:
            raise ValueError("review status must match its exact prompt/output hash bindings")
        return self

    @property
    def is_frozen(self) -> bool:
        return self.status == "FROZEN_HASH_BOUND"


class G04LicenseEvidenceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    license_name: Literal["Creative Commons Attribution-ShareAlike 4.0"]
    license_url: HttpUrl
    source_url: HttpUrl
    source_title: Literal["云台山", "张家界", "秦岭"]
    source_revision_id: int = Field(gt=0)
    attribution: str = Field(min_length=1, max_length=500)
    contains_embedded_media: Literal[False]
    contains_contact_details: Literal[False]
    contains_known_pii: Literal[False]

    @model_validator(mode="after")
    def validate_exact_revision(self) -> "G04LicenseEvidenceV1":
        source_url = str(self.source_url)
        if f"oldid={self.source_revision_id}" not in source_url:
            raise ValueError("source URL must bind the exact source revision")
        if self.source_title not in unquote(source_url):
            raise ValueError("source URL must bind the declared source title")
        return self


class G04CaptureProvenanceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provenance_kind: Literal["CAPTURE_PROVENANCE"]
    capture_status: Literal[
        "PENDING_CAPTURE",
        "PENDING_FRESH_REVIEW",
        "FROZEN_HASH_BOUND",
    ]
    exact_revision_url: HttpUrl
    source_revision_id: int = Field(gt=0)
    transform: Literal["CROP_ONLY"]
    capture_sha256: str = Field(pattern=rf"^(?:[0-9a-f]{{64}}|{PENDING_CAPTURE_HASH})$")
    source_file: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9._-]+\.(?:png|jpg|jpeg|webp)$",
    )
    source_size_bytes: int | None = Field(default=None, gt=0, le=10 * 1024 * 1024)
    image_width: int | None = Field(default=None, gt=0, le=10000)
    image_height: int | None = Field(default=None, gt=0, le=10000)
    captured_at: datetime | None = None
    capture_method: Literal["BROWSER_SCREENSHOT_THEN_CROP"] | None = None
    viewport_css_width: int | None = Field(default=None, gt=0, le=10000)
    viewport_css_height: int | None = Field(default=None, gt=0, le=10000)
    device_scale_factor: float | None = Field(default=None, gt=0, le=4)
    crop_xywh: tuple[int, int, int, int] | None = None
    paired_visible_text: str | None = Field(default=None, min_length=1, max_length=12000)
    paired_visible_text_sha256: str = Field(pattern=rf"^(?:[0-9a-f]{{64}}|{PENDING_REVIEW_HASH})$")
    transcription_source: Literal["FROZEN_SCREENSHOT_PIXELS_ONLY"]

    @model_validator(mode="after")
    def validate_capture_state(self) -> "G04CaptureProvenanceV1":
        if f"oldid={self.source_revision_id}" not in str(self.exact_revision_url):
            raise ValueError("capture provenance must bind an exact oldid revision")
        artifact_values = (
            self.source_file,
            self.source_size_bytes,
            self.image_width,
            self.image_height,
            self.captured_at,
            self.capture_method,
            self.viewport_css_width,
            self.viewport_css_height,
            self.device_scale_factor,
            self.crop_xywh,
        )
        if self.capture_status == "PENDING_CAPTURE":
            if self.capture_sha256 != PENDING_CAPTURE_HASH or any(value is not None for value in artifact_values):
                raise ValueError("pending capture must not claim unhashed capture artifacts")
            if self.paired_visible_text is not None or self.paired_visible_text_sha256 != PENDING_REVIEW_HASH:
                raise ValueError("pending capture cannot claim paired visible text")
            return self
        if self.capture_sha256 == PENDING_CAPTURE_HASH or any(value is None for value in artifact_values):
            raise ValueError("captured evidence requires hash, size, dimensions and time")
        assert self.crop_xywh is not None
        assert self.viewport_css_width is not None
        assert self.viewport_css_height is not None
        assert self.device_scale_factor is not None
        left, top, width, height = self.crop_xywh
        if min(left, top) < 0 or width <= 0 or height <= 0:
            raise ValueError("capture crop must be a positive physical-pixel rectangle")
        if width != self.image_width or height != self.image_height:
            raise ValueError("capture crop dimensions must match the frozen image")
        rendered_width = round(self.viewport_css_width * self.device_scale_factor)
        rendered_height = round(self.viewport_css_height * self.device_scale_factor)
        if left + width > rendered_width or top + height > rendered_height:
            raise ValueError("capture crop must stay inside the rendered viewport")
        if self.capture_status == "PENDING_FRESH_REVIEW":
            if self.paired_visible_text is not None or self.paired_visible_text_sha256 != PENDING_REVIEW_HASH:
                raise ValueError("pending review cannot claim a paired transcript")
            return self
        if self.paired_visible_text is None or self.paired_visible_text_sha256 == PENDING_REVIEW_HASH:
            raise ValueError("frozen capture requires exact paired visible text")
        expected = hashlib.sha256(self.paired_visible_text.encode("utf-8")).hexdigest()
        if self.paired_visible_text_sha256 != expected:
            raise ValueError("paired visible-text hash does not match the exact text")
        return self


class G04RenderProvenanceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provenance_kind: Literal["RENDER_PROVENANCE"]
    source_material_kind: Literal["DERIVED_TEXT_DAY"]
    derivative_disclosure: str = Field(min_length=1, max_length=800)
    source_text: str = Field(min_length=1, max_length=12000)
    source_text_sha256: str = Field(pattern=HASH_PATTERN)
    render_script_repository_path: str = Field(min_length=1)
    render_script_sha256: str = Field(pattern=HASH_PATTERN)
    source_file: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+\.(?:png|jpg|jpeg|webp)$")
    source_sha256: str = Field(pattern=HASH_PATTERN)
    source_size_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    image_width: Literal[1080]
    image_height: Literal[1920]

    @model_validator(mode="after")
    def validate_render_binding(self) -> "G04RenderProvenanceV1":
        expected = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        if self.source_text_sha256 != expected:
            raise ValueError("render source-text hash does not match the exact text")
        return self


class G04FrozenFieldV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,99}$")
    field_type: Literal[
        "DESTINATION",
        "DAY_MARKER",
        "ACTIVITY_PLACE",
        "TIME_HINT",
        "AREA_ADDRESS",
    ]
    expected_text: str = Field(min_length=1, max_length=200)
    region_xyxy: tuple[int, int, int, int]
    extraction: Literal[
        "WHOLE_LINE",
        "CONTAINS_EXPECTED_TEXT",
        "BEFORE_MIDDLE_DOT",
        "AFTER_MIDDLE_DOT",
    ]
    reading_order_index: int = Field(ge=0)
    must_confirm: bool = False
    activity_role: G04ActivityRole | None = None
    place_metric_eligibility: Literal["ELIGIBLE", "NOT_APPLICABLE"] = "NOT_APPLICABLE"

    @model_validator(mode="after")
    def validate_region_and_role(self) -> "G04FrozenFieldV1":
        left, top, right, bottom = self.region_xyxy
        if min(left, top) < 0 or left >= right or top >= bottom:
            raise ValueError("field region must be a positive xyxy rectangle")
        if right > 1080 or bottom > 1920:
            raise ValueError("field region must stay inside the frozen viewport")
        if self.field_type == "ACTIVITY_PLACE":
            if self.activity_role is None:
                raise ValueError("activity-place oracle must freeze activity_role")
            if self.place_metric_eligibility == "ELIGIBLE" and self.activity_role != "PLANNED":
                raise ValueError("only PLANNED activity places can be parity eligible")
            if self.must_confirm and self.place_metric_eligibility == "ELIGIBLE":
                raise ValueError("confirmation-controlled places cannot inflate parity")
        elif self.activity_role is not None or self.place_metric_eligibility != "NOT_APPLICABLE":
            raise ValueError("non-place fields must use NOT_APPLICABLE and no role")
        if self.extraction in {"BEFORE_MIDDLE_DOT", "AFTER_MIDDLE_DOT"} and self.field_type not in {
            "ACTIVITY_PLACE",
            "AREA_ADDRESS",
        }:
            raise ValueError("middle-dot extraction only applies to place/area fields")
        return self


class G04LicensedScreenshotCaseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,99}$")
    case_status: Literal[
        "PENDING_CAPTURE",
        "PENDING_FRESH_REVIEW",
        "FROZEN_HASH_BOUND",
        "DERIVED_NOT_EVALUABLE",
    ]
    evidence_tier: Literal[
        "LICENSED_REAL_SCREENSHOT",
        "LICENSED_DERIVED_TEXT_SCREENSHOT",
    ]
    metric_scope: tuple[G04MetricScope, ...] = Field(min_length=1)
    performance_member: Literal[False]
    license: G04LicenseEvidenceV1
    capture_provenance: G04CaptureProvenanceV1 | None = None
    render_provenance: G04RenderProvenanceV1 | None = None
    fields: tuple[G04FrozenFieldV1, ...] = ()
    forbidden_place_predictions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_case(self) -> "G04LicensedScreenshotCaseV1":
        if len(self.metric_scope) != len(set(self.metric_scope)):
            raise ValueError("case metric scopes must be unique")
        has_capture = self.capture_provenance is not None
        has_render = self.render_provenance is not None
        if has_capture == has_render:
            raise ValueError("capture_provenance and render_provenance form a strict exclusive union")
        if self.evidence_tier == "LICENSED_REAL_SCREENSHOT":
            if not has_capture or self.case_status == "DERIVED_NOT_EVALUABLE":
                raise ValueError("real screenshot evidence requires capture provenance")
            if not set(self.metric_scope) & {
                "REAL_OCR_READING",
                "REAL_PLANNED_PARITY",
                "REAL_LOW_CONFIDENCE_CONTROL",
            }:
                raise ValueError("real screenshots require a real metric scope")
            capture = self.capture_provenance
            assert capture is not None
            if capture.source_revision_id != self.license.source_revision_id:
                raise ValueError("capture and license revision bindings must match")
            if str(capture.exact_revision_url) != str(self.license.source_url):
                raise ValueError("capture and license exact-revision URLs must match")
            if self.case_status != capture.capture_status:
                raise ValueError("case and capture readiness states must match")
        else:
            if not has_render or self.case_status != "DERIVED_NOT_EVALUABLE":
                raise ValueError("derived screenshots require render provenance")
            if set(self.metric_scope) & {"REAL_OCR_READING", "REAL_PLANNED_PARITY"}:
                raise ValueError("derived screenshots cannot claim real metric scopes")

        if self.case_status != "FROZEN_HASH_BOUND":
            if self.fields or self.forbidden_place_predictions:
                raise ValueError("pending evidence cannot claim frozen oracle outputs")
            return self

        if len(self.fields) < 2:
            raise ValueError("frozen real cases require at least two OCR fields")
        if any(
            field.region_xyxy[2] > self.image_width or field.region_xyxy[3] > self.image_height for field in self.fields
        ):
            raise ValueError("quality field geometry must stay inside its exact capture")
        field_ids = [item.field_id for item in self.fields]
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("frozen field IDs must be unique within a case")
        order = [item.reading_order_index for item in self.fields]
        if order != list(range(len(order))):
            raise ValueError("reading-order indices must be contiguous and declared in order")
        activities = [item for item in self.fields if item.field_type == "ACTIVITY_PLACE"]
        if not activities:
            raise ValueError("each frozen real case must contain an activity-place oracle")
        if "REAL_PLANNED_PARITY" in self.metric_scope and not any(
            item.activity_role == "PLANNED" and item.place_metric_eligibility == "ELIGIBLE" for item in activities
        ):
            raise ValueError("planned parity scope requires eligible PLANNED oracle items")
        if "REAL_LOW_CONFIDENCE_CONTROL" in self.metric_scope:
            if set(self.metric_scope) != {"REAL_LOW_CONFIDENCE_CONTROL"}:
                raise ValueError("low-confidence control scope must remain isolated")
            if not all(field.must_confirm for field in self.fields):
                raise ValueError("every low-confidence control field must require confirmation")
            if any(field.place_metric_eligibility == "ELIGIBLE" for field in self.fields):
                raise ValueError("low-confidence controls cannot enter place parity")
        if "REFERENCE_CONTROL" in self.metric_scope and not any(
            item.activity_role == "REFERENCE" and item.place_metric_eligibility == "NOT_APPLICABLE"
            for item in activities
        ):
            raise ValueError("reference scope requires a NOT_APPLICABLE REFERENCE oracle")
        if not self.forbidden_place_predictions:
            raise ValueError("frozen cases require serious-error control strings")
        return self

    @property
    def source_file(self) -> str:
        provenance = self.capture_provenance or self.render_provenance
        value = provenance.source_file if provenance is not None else None
        if value is None:
            raise ValueError("source file is not frozen")
        return value

    @property
    def source_sha256(self) -> str:
        if self.capture_provenance is not None:
            value = self.capture_provenance.capture_sha256
            if value == PENDING_CAPTURE_HASH:
                raise ValueError("capture hash is not frozen")
            return value
        assert self.render_provenance is not None
        return self.render_provenance.source_sha256

    @property
    def source_size_bytes(self) -> int:
        provenance = self.capture_provenance or self.render_provenance
        value = provenance.source_size_bytes if provenance is not None else None
        if value is None:
            raise ValueError("source size is not frozen")
        return value

    @property
    def image_width(self) -> int:
        provenance = self.capture_provenance or self.render_provenance
        value = provenance.image_width if provenance is not None else None
        if value is None:
            raise ValueError("source width is not frozen")
        return value

    @property
    def image_height(self) -> int:
        provenance = self.capture_provenance or self.render_provenance
        value = provenance.image_height if provenance is not None else None
        if value is None:
            raise ValueError("source height is not frozen")
        return value

    @property
    def paired_visible_text(self) -> str:
        if self.capture_provenance is None or self.capture_provenance.paired_visible_text is None:
            raise ValueError("paired visible text is not frozen")
        return self.capture_provenance.paired_visible_text

    @property
    def parity_places(self) -> tuple[str, ...]:
        return tuple(
            item.expected_text
            for item in self.fields
            if item.field_type == "ACTIVITY_PLACE"
            and item.activity_role == "PLANNED"
            and item.place_metric_eligibility == "ELIGIBLE"
        )


class G04SyntheticFormatCaseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,99}$")
    format_kind: Literal["CHAT", "MEMO", "GUIDE", "AI_REPLY"]
    image_count: Literal[1, 3, 6]
    theme: Literal["LIGHT", "DARK"]
    expected_coverage: tuple[
        Literal["DESTINATION", "DAY_MARKER", "ACTIVITY_PLACE", "TIME_HINT", "AREA_ADDRESS"],
        ...,
    ] = Field(min_length=1)


class G04SyntheticPerformanceCaseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,99}$")
    evidence_tier: Literal["SYNTHETIC_PERFORMANCE_ONLY"]
    metric_scope: Literal["SYNTHETIC_PERFORMANCE_ONLY"]
    quality_metric_eligibility: Literal["NOT_APPLICABLE"]
    source_file: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+\.png$")
    source_sha256: str = Field(pattern=HASH_PATTERN)
    source_size_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    image_width: Literal[1080]
    image_height: Literal[1920]
    render_script_repository_path: Literal["frontend/e2e/support/render-g04-parity-fixtures.js"]
    render_script_sha256: str = Field(pattern=HASH_PATTERN)
    deterministic_render: Literal[True]
    originals_in_git: Literal[False]


def oracle_projection_payload(
    cases: tuple[G04LicensedScreenshotCaseV1, ...],
) -> list[dict[str, object]]:
    """Canonical adjudicated truth that the baseline is not allowed to edit."""

    return [
        {
            "case_id": case.case_id,
            "source_file": case.source_file,
            "source_sha256": case.source_sha256,
            "paired_visible_text": case.paired_visible_text,
            "fields": [field.model_dump(mode="json") for field in case.fields],
            "forbidden_place_predictions": list(case.forbidden_place_predictions),
        }
        for case in cases
    ]


class G04LicensedScreenshotBaselineV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["g04-licensed-screenshot-baseline-v2"]
    dataset_id: str = Field(pattern=r"^g04-[a-z0-9-]+-v[0-9]+$")
    parity_status: Literal[
        "PENDING_CAPTURE",
        "PENDING_FRESH_REVIEW",
        "NOT_EVALUABLE_DERIVED_ONLY",
        "EVALUABLE_LICENSED_REAL",
    ]
    review: G04ReviewEvidenceV1
    cases: tuple[G04LicensedScreenshotCaseV1, ...] = Field(min_length=4, max_length=4)
    synthetic_performance_cases: tuple[
        G04SyntheticPerformanceCaseV1,
        G04SyntheticPerformanceCaseV1,
        G04SyntheticPerformanceCaseV1,
    ]
    synthetic_format_cases: tuple[G04SyntheticFormatCaseV1, ...] = Field(min_length=4)

    @model_validator(mode="after")
    def validate_dataset(self) -> "G04LicensedScreenshotBaselineV1":
        case_ids = [item.case_id for item in self.cases]
        performance_ids = [item.case_id for item in self.synthetic_performance_cases]
        synthetic_ids = [item.case_id for item in self.synthetic_format_cases]
        all_ids = (*case_ids, *performance_ids, *synthetic_ids)
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("quality, performance and format case IDs must be unique")
        performance_files = {item.source_file for item in self.synthetic_performance_cases}
        if performance_files != {"beijing.png", "shanghai.png", "hangzhou.png"}:
            raise ValueError("performance-only set must bind the three deterministic renders")
        performance_hashes = {item.source_sha256 for item in self.synthetic_performance_cases}
        if len(performance_hashes) != 3:
            raise ValueError("performance-only render hashes must be unique")
        render_hashes = {item.render_script_sha256 for item in self.synthetic_performance_cases}
        if len(render_hashes) != 1:
            raise ValueError("performance-only renders must share one exact renderer")
        revisions = {(item.license.source_title, item.license.source_revision_id) for item in self.cases}
        if revisions != {("云台山", 220410), ("张家界", 221547), ("秦岭", 133737)}:
            raise ValueError("formal baseline must bind the three approved exact revisions")
        quality_cases = [
            item
            for item in self.cases
            if {
                "REAL_OCR_READING",
                "REAL_PLANNED_PARITY",
            }
            <= set(item.metric_scope)
        ]
        low_confidence_controls = [
            item
            for item in self.cases
            if set(item.metric_scope) == {"REAL_LOW_CONFIDENCE_CONTROL"}
        ]
        if len(quality_cases) != 3 or len(low_confidence_controls) != 1:
            raise ValueError(
                "formal baseline requires three real quality cases and one isolated low-confidence control"
            )
        states = {item.case_status for item in self.cases}
        expected_status: str | None = None
        if states == {"PENDING_CAPTURE"}:
            expected_status = "PENDING_CAPTURE"
        elif states == {"PENDING_FRESH_REVIEW"}:
            expected_status = "PENDING_FRESH_REVIEW"
        elif states == {"DERIVED_NOT_EVALUABLE"}:
            expected_status = "NOT_EVALUABLE_DERIVED_ONLY"
        elif states == {"FROZEN_HASH_BOUND"} and self.review.is_frozen:
            expected_status = "EVALUABLE_LICENSED_REAL"
        if expected_status is None or self.parity_status != expected_status:
            raise ValueError("dataset parity status must match one unmixed readiness state")
        if self.parity_status == "EVALUABLE_LICENSED_REAL":
            if not all(item.evidence_tier == "LICENSED_REAL_SCREENSHOT" for item in self.cases):
                raise ValueError("formal evaluability requires only real screenshots")
            confirmation_fields = [field for case in self.cases for field in case.fields if field.must_confirm]
            if not confirmation_fields:
                raise ValueError("formal OCR gate requires a non-vacuous low-confidence control")
            if any(
                field.must_confirm
                for case in quality_cases
                for field in case.fields
            ):
                raise ValueError("quality cases cannot hide low-confidence controls in parity metrics")
            observed_projection_hash = canonical_sha256(
                oracle_projection_payload(self.cases)
            )
            if observed_projection_hash != self.review.oracle_projection_sha256:
                raise ValueError(
                    "baseline oracle projection must exactly match adjudicated evidence"
                )
        elif self.review.is_frozen:
            raise ValueError("pending or derived datasets cannot claim a frozen review")
        covered = {field_type for case in self.synthetic_format_cases for field_type in case.expected_coverage}
        if covered != {
            "DESTINATION",
            "DAY_MARKER",
            "ACTIVITY_PLACE",
            "TIME_HINT",
            "AREA_ADDRESS",
        }:
            raise ValueError("synthetic format cases must cover all frozen field types")
        return self

    @property
    def synthetic_spec_sha256(self) -> str:
        return canonical_sha256([item.model_dump(mode="json") for item in self.synthetic_format_cases])

    @property
    def baseline_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))
