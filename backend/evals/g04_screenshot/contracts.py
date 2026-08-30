from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class G04SourceEvidenceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_tier: Literal["LICENSED_REAL_SOURCE", "SYNTHETIC_FORMAT_ONLY"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_name: str | None = Field(default=None, min_length=1)
    license_reference: str | None = Field(default=None, min_length=1)
    synthetic_spec_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> "G04SourceEvidenceV1":
        if self.evidence_tier == "LICENSED_REAL_SOURCE":
            if not self.license_name or not self.license_reference:
                raise ValueError("licensed real sources require license evidence")
            if self.synthetic_spec_sha256 is not None:
                raise ValueError("real-source evidence cannot use a synthetic spec")
        else:
            if self.synthetic_spec_sha256 is None:
                raise ValueError("synthetic format evidence requires its frozen spec hash")
            if self.license_name is not None or self.license_reference is not None:
                raise ValueError("synthetic format evidence must not claim a source license")
        return self


class G04SeriousErrorV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: Literal[
        "WRONG_CITY",
        "WRONG_CATEGORY",
        "DESCRIPTION_AS_PLACE",
        "URL_AS_PLACE",
    ]
    item_id: str = Field(min_length=1, max_length=200)


class G04ScreenshotParityCaseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,99}$")
    source: G04SourceEvidenceV1
    expected_key_fields: tuple[str, ...] = Field(min_length=1)
    observed_key_fields: tuple[str, ...]
    expected_reading_order: tuple[str, ...] = Field(min_length=1)
    observed_reading_order: tuple[str, ...]
    expected_low_confidence_fields: tuple[str, ...]
    observed_confirmation_fields: tuple[str, ...]
    reference_executable_places: tuple[str, ...] = Field(min_length=1)
    text_executable_places: tuple[str, ...]
    screenshot_executable_places: tuple[str, ...]
    serious_errors: tuple[G04SeriousErrorV1, ...]

    @model_validator(mode="after")
    def validate_unique_items(self) -> "G04ScreenshotParityCaseV1":
        fields = (
            "expected_key_fields",
            "observed_key_fields",
            "expected_reading_order",
            "observed_reading_order",
            "expected_low_confidence_fields",
            "observed_confirmation_fields",
            "reference_executable_places",
            "text_executable_places",
            "screenshot_executable_places",
        )
        for field_name in fields:
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicate identifiers")
        return self


class G04PerformanceEvidenceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    warmup_runs: Literal[2]
    measured_runs: Literal[20]
    image_count: Literal[3]
    image_width: Literal[1080]
    image_height: Literal[1920]
    max_concurrency: Literal[1]
    durations_ms: tuple[float, ...] = Field(min_length=20, max_length=20)

    @model_validator(mode="after")
    def validate_durations(self) -> "G04PerformanceEvidenceV1":
        if any(value < 0 for value in self.durations_ms):
            raise ValueError("performance durations must be non-negative")
        return self


class G04ScreenshotParityManifestV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["g04-screenshot-parity-manifest-v1"]
    cases: tuple[G04ScreenshotParityCaseV1, ...] = Field(min_length=1)
    performance: G04PerformanceEvidenceV1

    @model_validator(mode="after")
    def validate_case_ids(self) -> "G04ScreenshotParityManifestV1":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("G04 manifest case IDs must be unique")
        return self


class G04ScreenshotScoreReportV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["g04-screenshot-score-report-v1"] = (
        "g04-screenshot-score-report-v1"
    )
    licensed_real_case_count: int = Field(ge=0)
    synthetic_format_case_count: int = Field(ge=0)
    key_field_precision: float = Field(ge=0, le=1)
    key_field_recall: float = Field(ge=0, le=1)
    key_field_f1: float = Field(ge=0, le=1)
    adjacency_precision: float = Field(ge=0, le=1)
    adjacency_recall: float = Field(ge=0, le=1)
    adjacency_f1: float = Field(ge=0, le=1)
    low_confidence_confirmation_recall: float = Field(ge=0, le=1)
    text_place_precision: float = Field(ge=0, le=1)
    text_place_recall: float = Field(ge=0, le=1)
    screenshot_place_precision: float = Field(ge=0, le=1)
    screenshot_place_recall: float = Field(ge=0, le=1)
    place_precision_drop_pp: float = Field(ge=0)
    place_recall_drop_pp: float = Field(ge=0)
    serious_error_count: int = Field(ge=0)
    performance_sample_count: int = Field(ge=0)
    three_image_p95_ms: float = Field(ge=0)
    failures: tuple[str, ...]
    gate_pass: bool
