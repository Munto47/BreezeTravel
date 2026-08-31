from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


G04MetricScope = Literal[
    "REAL_OCR_READING",
    "REAL_PLANNED_PARITY",
    "REAL_LOW_CONFIDENCE_CONTROL",
    "REFERENCE_CONTROL",
    "SYNTHETIC_FORMAT_CONTROL",
]
G04ActivityRole = Literal[
    "PLANNED",
    "OPTIONAL",
    "REFERENCE",
    "EXCLUDED",
    "PASS_THROUGH",
]


class G04SourceEvidenceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_tier: Literal[
        "LICENSED_REAL_SCREENSHOT",
        "LICENSED_DERIVED_TEXT_SCREENSHOT",
        "SYNTHETIC_FORMAT_ONLY",
    ]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_name: str | None = Field(default=None, min_length=1)
    license_reference: str | None = Field(default=None, min_length=1)
    synthetic_spec_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> "G04SourceEvidenceV1":
        if self.evidence_tier in {
            "LICENSED_REAL_SCREENSHOT",
            "LICENSED_DERIVED_TEXT_SCREENSHOT",
        }:
            if not self.license_name or not self.license_reference:
                raise ValueError("licensed source evidence requires license metadata")
            if self.synthetic_spec_sha256 is not None:
                raise ValueError("licensed evidence cannot use a synthetic spec")
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


class G04PlaceOracleItemV1(BaseModel):
    """Frozen place truth, including roles that must never enter parity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    oracle_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,99}$")
    expected_text: str = Field(min_length=1, max_length=200)
    activity_role: G04ActivityRole
    metric_eligibility: Literal["ELIGIBLE", "NOT_APPLICABLE"]

    @model_validator(mode="after")
    def validate_role_eligibility(self) -> "G04PlaceOracleItemV1":
        if self.metric_eligibility == "ELIGIBLE" and self.activity_role != "PLANNED":
            raise ValueError("only PLANNED place-oracle items can be parity eligible")
        return self


class G04EvaluatedPlaceMetricV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["EVALUATED"]
    oracle_items: tuple[G04PlaceOracleItemV1, ...] = Field(min_length=1)
    reference_executable_places: tuple[str, ...] = Field(min_length=1)
    text_executable_places: tuple[str, ...] = Field(min_length=1)
    screenshot_executable_places: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evaluated_metric(self) -> "G04EvaluatedPlaceMetricV1":
        oracle_ids = [item.oracle_id for item in self.oracle_items]
        if len(oracle_ids) != len(set(oracle_ids)):
            raise ValueError("place oracle IDs must be unique within a case")
        eligible = tuple(
            item.expected_text
            for item in self.oracle_items
            if item.metric_eligibility == "ELIGIBLE"
        )
        if not eligible:
            raise ValueError("evaluated place parity requires eligible PLANNED oracle items")
        if self.reference_executable_places != eligible:
            raise ValueError(
                "place reference must exactly equal eligible PLANNED oracle items"
            )
        for name in (
            "reference_executable_places",
            "text_executable_places",
            "screenshot_executable_places",
        ):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicate values")
        return self


class G04NotApplicablePlaceMetricV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["NOT_APPLICABLE"]
    reason: Literal[
        "OCR_ONLY_SCOPE",
        "REFERENCE_CONTROL_ROLE",
        "LOW_CONFIDENCE_CONTROL",
        "SYNTHETIC_FORMAT_ONLY",
        "DERIVED_NOT_EVALUABLE",
    ]
    oracle_items: tuple[G04PlaceOracleItemV1, ...] = ()
    reference_executable_places: Literal["NOT_APPLICABLE"]
    text_executable_places: Literal["NOT_APPLICABLE"]
    screenshot_executable_places: Literal["NOT_APPLICABLE"]

    @model_validator(mode="after")
    def validate_not_applicable_metric(self) -> "G04NotApplicablePlaceMetricV1":
        oracle_ids = [item.oracle_id for item in self.oracle_items]
        if len(oracle_ids) != len(set(oracle_ids)):
            raise ValueError("place oracle IDs must be unique within a case")
        if any(item.metric_eligibility != "NOT_APPLICABLE" for item in self.oracle_items):
            raise ValueError("NOT_APPLICABLE place metrics cannot hide eligible items")
        if self.reason in {"OCR_ONLY_SCOPE", "REFERENCE_CONTROL_ROLE"} and not self.oracle_items:
            raise ValueError("real NOT_APPLICABLE place controls require a frozen oracle item")
        if self.reason == "REFERENCE_CONTROL_ROLE" and any(
            item.activity_role != "REFERENCE" for item in self.oracle_items
        ):
            raise ValueError("reference controls must freeze the REFERENCE activity role")
        if self.reason == "SYNTHETIC_FORMAT_ONLY" and self.oracle_items:
            raise ValueError("synthetic format controls cannot claim a place oracle")
        return self


G04PlaceMetricV1 = Annotated[
    G04EvaluatedPlaceMetricV1 | G04NotApplicablePlaceMetricV1,
    Field(discriminator="status"),
]


class G04ScreenshotParityCaseV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,99}$")
    source: G04SourceEvidenceV1
    metric_scope: tuple[G04MetricScope, ...] = Field(min_length=1)
    expected_key_fields: tuple[str, ...] = Field(min_length=1)
    observed_key_fields: tuple[str, ...]
    expected_reading_order: tuple[str, ...] = Field(min_length=1)
    observed_reading_order: tuple[str, ...]
    expected_low_confidence_fields: tuple[str, ...]
    observed_confirmation_fields: tuple[str, ...]
    place_metric: G04PlaceMetricV1
    serious_errors: tuple[G04SeriousErrorV1, ...]

    @model_validator(mode="after")
    def validate_case_contract(self) -> "G04ScreenshotParityCaseV1":
        if len(self.metric_scope) != len(set(self.metric_scope)):
            raise ValueError("metric scopes must be unique within a case")
        fields = (
            "expected_key_fields",
            "observed_key_fields",
            "expected_reading_order",
            "observed_reading_order",
            "expected_low_confidence_fields",
            "observed_confirmation_fields",
        )
        for field_name in fields:
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicate identifiers")

        real_scopes = {
            "REAL_OCR_READING",
            "REAL_PLANNED_PARITY",
            "REAL_LOW_CONFIDENCE_CONTROL",
        }
        observed_scopes = set(self.metric_scope)
        if self.source.evidence_tier == "LICENSED_REAL_SCREENSHOT":
            if not observed_scopes & real_scopes:
                raise ValueError("licensed real screenshots require a real metric scope")
        elif observed_scopes & real_scopes:
            raise ValueError("derived and synthetic cases cannot claim real metric scopes")

        if "REAL_OCR_READING" in observed_scopes and len(self.expected_reading_order) < 2:
            raise ValueError(
                "real OCR cases require at least one reading-order adjacency"
            )
        if "REAL_PLANNED_PARITY" in observed_scopes:
            if self.place_metric.status != "EVALUATED":
                raise ValueError("planned parity scope requires an evaluated place metric")
        elif self.place_metric.status != "NOT_APPLICABLE":
            raise ValueError("non-parity cases must use explicit NOT_APPLICABLE place fields")

        if "REAL_LOW_CONFIDENCE_CONTROL" in observed_scopes:
            if observed_scopes != {"REAL_LOW_CONFIDENCE_CONTROL"}:
                raise ValueError("low-confidence control scope must remain isolated")
            if not self.expected_low_confidence_fields:
                raise ValueError("low-confidence control must have a non-empty denominator")
            if (
                self.place_metric.status != "NOT_APPLICABLE"
                or self.place_metric.reason != "LOW_CONFIDENCE_CONTROL"
            ):
                raise ValueError("low-confidence control requires an explicit NOT_APPLICABLE reason")

        if (
            "REFERENCE_CONTROL" in observed_scopes
            and self.source.evidence_tier == "LICENSED_REAL_SCREENSHOT"
            and not any(
                item.activity_role == "REFERENCE"
                and item.metric_eligibility == "NOT_APPLICABLE"
                for item in self.place_metric.oracle_items
            )
        ):
            raise ValueError(
                "real reference controls must freeze a non-eligible REFERENCE oracle"
            )
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
        if any(value <= 0 for value in self.durations_ms):
            raise ValueError("performance durations must be positive")
        if len(self.durations_ms) != self.measured_runs:
            raise ValueError("every measured run must have one positive duration")
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
    licensed_real_ocr_case_count: int = Field(ge=0)
    licensed_real_planned_parity_case_count: int = Field(ge=0)
    reference_control_case_count: int = Field(ge=0)
    synthetic_format_case_count: int = Field(ge=0)
    key_field_expected_count: int = Field(ge=0)
    key_field_precision: float = Field(ge=0, le=1)
    key_field_recall: float = Field(ge=0, le=1)
    key_field_f1: float = Field(ge=0, le=1)
    adjacency_precision: float = Field(ge=0, le=1)
    adjacency_recall: float = Field(ge=0, le=1)
    adjacency_f1: float = Field(ge=0, le=1)
    reading_adjacency_expected_count: int = Field(ge=0)
    low_confidence_expected_count: int = Field(ge=0)
    low_confidence_confirmation_recall: float = Field(ge=0, le=1)
    reference_place_count: int = Field(ge=0)
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
