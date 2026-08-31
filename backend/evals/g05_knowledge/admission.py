from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_SOURCE_TIERS = {"GOVERNMENT", "OFFICIAL_OPERATOR"}
ADMITTED_LICENSE_STATES = {"FACTS_ONLY_WITH_ATTRIBUTION", "OPEN_DATA_REUSE"}
ALLOWED_STORAGE_POLICIES = {"NORMALIZED_FACTS_ONLY", "NO_STORAGE"}
ALLOWED_CLAIM_TYPES = {
    "TYPICAL_DURATION",
    "SUITABLE_TIME",
    "NIGHT_VIEW",
    "SEASON",
    "RESERVATION_ADVICE",
}
REQUIRED_CITIES = {"北京", "上海", "杭州"}
REQUIRED_JOURNEYS = {"G01-TC-001", "G01-TC-013", "G01-TC-025"}
FORBIDDEN_KEYS = {
    "raw_html",
    "raw_body",
    "page_text",
    "full_text",
    "screenshot",
    "creator_content",
}


@dataclass(frozen=True)
class AdmissionReport:
    place_count: int
    available_place_count: int
    explicit_gap_count: int
    admitted_source_count: int
    not_ready_source_count: int
    claim_count: int
    available_claim_type_count: int
    explicit_gap_claim_type_count: int
    required_field_coverage: float
    source_binding_coverage: float
    unauthorized_claim_count: int
    expired_claim_count: int
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return (
            not self.errors
            and self.required_field_coverage == 1.0
            and self.source_binding_coverage == 1.0
            and self.unauthorized_claim_count == 0
            and self.expired_claim_count == 0
        )


def load_admission_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_https(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return (
        value == value.strip()
        and parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
    )


def _required(
    record: dict[str, Any],
    names: tuple[str, ...],
    label: str,
    errors: list[str],
) -> int:
    missing = [name for name in names if record.get(name) in (None, "", [])]
    if missing:
        errors.append(f"{label} missing required fields: {', '.join(missing)}")
    return len(names) - len(missing)


def _find_forbidden_keys(value: object, *, path: str = "root") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key.lower() in FORBIDDEN_KEYS:
                found.append(child_path)
            found.extend(_find_forbidden_keys(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_find_forbidden_keys(child, path=f"{path}[{index}]"))
    return found


def evaluate_admission_manifest(
    manifest: dict[str, Any],
    *,
    as_of: datetime,
) -> AdmissionReport:
    errors: list[str] = []
    required_checks = 0
    required_passes = 0

    if manifest.get("schema_version") != "g05-knowledge-admission-v1":
        errors.append("unsupported admission manifest schema")
    if set(manifest.get("validation_journeys", [])) != REQUIRED_JOURNEYS:
        errors.append("validation journeys must remain bound to G01-TC-001/013/025")

    forbidden = _find_forbidden_keys(manifest)
    if forbidden:
        errors.append("full source content is forbidden: " + ", ".join(forbidden))

    places = manifest.get("places", [])
    sources = manifest.get("sources", [])
    claims = manifest.get("claims", [])
    claim_type_dispositions = manifest.get("claim_type_dispositions", [])
    if not isinstance(places, list) or not isinstance(sources, list) or not isinstance(claims, list):
        errors.append("places, sources and claims must be arrays")
        places, sources, claims = [], [], []
    if not isinstance(claim_type_dispositions, list):
        errors.append("claim_type_dispositions must be an array")
        claim_type_dispositions = []

    disposition_by_type: dict[str, str] = {}
    available_claim_type_count = 0
    explicit_gap_claim_type_count = 0
    for index, disposition_record in enumerate(claim_type_dispositions):
        label = f"claim_type_disposition[{index}]"
        fields = ("claim_type", "disposition", "reason")
        required_checks += len(fields)
        required_passes += _required(disposition_record, fields, label, errors)
        claim_type = disposition_record.get("claim_type")
        disposition = disposition_record.get("disposition")
        if claim_type not in ALLOWED_CLAIM_TYPES:
            errors.append(f"{label} has unsupported claim type")
            continue
        if claim_type in disposition_by_type:
            errors.append(f"duplicate claim type disposition: {claim_type}")
        disposition_by_type[claim_type] = disposition
        if disposition == "CLAIM_AVAILABLE":
            available_claim_type_count += 1
        elif disposition == "EXPLICIT_GAP":
            explicit_gap_claim_type_count += 1
        else:
            errors.append(f"{label} has invalid disposition")
    if set(disposition_by_type) != ALLOWED_CLAIM_TYPES:
        errors.append("all five fixed claim types require an explicit disposition")

    if len(places) != 18:
        errors.append(f"frozen validation set must contain 18 places, found {len(places)}")
    place_names = [place.get("canonical_name") for place in places]
    if len(set(place_names)) != len(place_names):
        errors.append("validation place names must be unique")
    if {place.get("city") for place in places} != REQUIRED_CITIES:
        errors.append("validation set must cover Beijing, Shanghai and Hangzhou")
    if {place.get("journey_id") for place in places} != REQUIRED_JOURNEYS:
        errors.append("every frozen journey must contribute places")

    place_by_key: dict[str, dict[str, Any]] = {}
    available_place_count = 0
    explicit_gap_count = 0
    for index, place in enumerate(places):
        label = f"place[{index}]"
        fields = ("place_key", "journey_id", "city", "canonical_name", "disposition", "reason")
        required_checks += len(fields)
        required_passes += _required(place, fields, label, errors)
        key = place.get("place_key")
        if isinstance(key, str):
            place_by_key[key] = place
        disposition = place.get("disposition")
        claim_keys = place.get("claim_keys", [])
        if disposition == "CLAIM_AVAILABLE":
            available_place_count += 1
            if not place.get("canonical_place_id") or not claim_keys:
                errors.append(f"{label} CLAIM_AVAILABLE requires canonical_place_id and claim_keys")
        elif disposition == "EXPLICIT_GAP":
            explicit_gap_count += 1
            if claim_keys:
                errors.append(f"{label} EXPLICIT_GAP cannot reference claims")
        else:
            errors.append(f"{label} has invalid disposition")

    source_by_version: dict[tuple[str, int], dict[str, Any]] = {}
    admitted_source_count = 0
    not_ready_source_count = 0
    for index, source in enumerate(sources):
        label = f"source[{index}]"
        fields = (
            "source_key",
            "version",
            "publisher_name",
            "source_tier",
            "canonical_url",
            "access_method",
            "terms_url",
            "license_status",
            "storage_policy",
            "admission_status",
            "observed_at",
            "reviewed_at",
            "expires_at",
            "reviewer",
            "withdrawal_method",
            "version_note",
        )
        required_checks += len(fields)
        required_passes += _required(source, fields, label, errors)
        key = (source.get("source_key"), source.get("version"))
        if key in source_by_version:
            errors.append(f"duplicate source version: {key}")
        source_by_version[key] = source
        if not _is_https(source.get("canonical_url")) or not _is_https(source.get("terms_url")):
            errors.append(f"{label} URLs must be HTTPS")
        if source.get("source_tier") not in ALLOWED_SOURCE_TIERS:
            errors.append(f"{label} source tier is not admitted for G05 v1")
        if source.get("storage_policy") not in ALLOWED_STORAGE_POLICIES:
            errors.append(f"{label} storage policy is not allowed")
        status = source.get("admission_status")
        if status == "ADMITTED":
            admitted_source_count += 1
            if source.get("license_status") not in ADMITTED_LICENSE_STATES:
                errors.append(f"{label} admitted without a reusable license basis")
            if source.get("storage_policy") != "NORMALIZED_FACTS_ONLY":
                errors.append(f"{label} admitted sources must store normalized facts only")
        elif status == "NOT_READY":
            not_ready_source_count += 1
            if source.get("storage_policy") != "NO_STORAGE":
                errors.append(f"{label} NOT_READY source must have NO_STORAGE policy")
        else:
            errors.append(f"{label} has invalid admission status")
        try:
            observed_at = datetime.fromisoformat(
                str(source["observed_at"]).replace("Z", "+00:00")
            )
            reviewed_at = datetime.fromisoformat(
                str(source["reviewed_at"]).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(source["expires_at"]).replace("Z", "+00:00")
            )
            if not observed_at <= reviewed_at < expires_at:
                errors.append(f"{label} observed/reviewed/expires order is invalid")
            if status == "ADMITTED" and not reviewed_at <= as_of < expires_at:
                errors.append(f"{label} admitted source is not current at the gate time")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} has invalid observed/reviewed/expires timestamps")

    claim_by_key: dict[str, dict[str, Any]] = {}
    unauthorized_claim_count = 0
    expired_claim_count = 0
    for index, claim in enumerate(claims):
        label = f"claim[{index}]"
        fields = (
            "claim_key",
            "version",
            "place_key",
            "canonical_place_id",
            "city",
            "claim_type",
            "conditions",
            "suggestion_text",
            "short_evidence",
            "source_key",
            "source_version",
            "effective_at",
            "expires_at",
            "reviewer",
        )
        required_checks += len(fields)
        required_passes += _required(claim, fields, label, errors)
        claim_key = claim.get("claim_key")
        if isinstance(claim_key, str):
            if claim_key in claim_by_key:
                errors.append(f"duplicate claim key: {claim_key}")
            claim_by_key[claim_key] = claim
        place = place_by_key.get(claim.get("place_key"))
        if place is None or place.get("disposition") != "CLAIM_AVAILABLE":
            errors.append(f"{label} is not bound to a CLAIM_AVAILABLE validation place")
        elif (
            claim.get("canonical_place_id") != place.get("canonical_place_id")
            or claim.get("city") != place.get("city")
        ):
            errors.append(f"{label} canonical place binding does not match validation set")
        if claim.get("claim_type") not in ALLOWED_CLAIM_TYPES:
            errors.append(f"{label} has unsupported claim type")
        source = source_by_version.get((claim.get("source_key"), claim.get("source_version")))
        if source is None or source.get("admission_status") != "ADMITTED":
            unauthorized_claim_count += 1
        try:
            effective_at = datetime.fromisoformat(str(claim["effective_at"]).replace("Z", "+00:00"))
            expires_at = datetime.fromisoformat(str(claim["expires_at"]).replace("Z", "+00:00"))
            if not effective_at <= as_of < expires_at:
                expired_claim_count += 1
            if effective_at >= expires_at:
                errors.append(f"{label} effective_at must precede expires_at")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} has invalid effective/expires timestamps")

    for index, place in enumerate(places):
        for claim_key in place.get("claim_keys", []):
            claim = claim_by_key.get(claim_key)
            if claim is None or claim.get("place_key") != place.get("place_key"):
                errors.append(f"place[{index}] references a missing or differently bound claim")

    actual_claim_types = {claim.get("claim_type") for claim in claims}
    for claim_type, disposition in disposition_by_type.items():
        if disposition == "CLAIM_AVAILABLE" and claim_type not in actual_claim_types:
            errors.append(f"claim type {claim_type} is marked available without a claim")
        if disposition == "EXPLICIT_GAP" and claim_type in actual_claim_types:
            errors.append(f"claim type {claim_type} is marked as a gap but has a claim")

    return AdmissionReport(
        place_count=len(places),
        available_place_count=available_place_count,
        explicit_gap_count=explicit_gap_count,
        admitted_source_count=admitted_source_count,
        not_ready_source_count=not_ready_source_count,
        claim_count=len(claims),
        available_claim_type_count=available_claim_type_count,
        explicit_gap_claim_type_count=explicit_gap_claim_type_count,
        required_field_coverage=(required_passes / required_checks if required_checks else 0.0),
        source_binding_coverage=(
            (len(claims) - unauthorized_claim_count) / len(claims) if claims else 0.0
        ),
        unauthorized_claim_count=unauthorized_claim_count,
        expired_claim_count=expired_claim_count,
        errors=tuple(errors),
    )
