"""Validate the dual-entry test corpus without executing the product.

This is the G0 structural preflight.  It deliberately distinguishes a valid
development corpus from a release-ready blind gate.  Passing this script does
not imply that the SUT, Providers, Judge, browser flows, or human calibration
have passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = BACKEND_ROOT / "eval_data" / "dual_entry_v1"
RUN_SPEC_ROOT = BACKEND_ROOT / "evals" / "run_specs"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


def _schema_errors(instance: dict[str, Any], schema: dict[str, Any], prefix: str) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{prefix}:{location}: {error.message}")
    return errors


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _nested_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _nested_keys(item)}
    return set()


def _nested_provider_canonical_ids(value: Any) -> set[str]:
    """Extract only explicit Provider receipt identities, never names/coords."""

    result: set[str] = set()
    if isinstance(value, dict):
        receipt = value.get("provider_receipt")
        if isinstance(receipt, dict):
            canonical_id = receipt.get("canonical_place_id")
            if isinstance(canonical_id, str) and canonical_id:
                result.add(canonical_id)
        for child in value.values():
            result.update(_nested_provider_canonical_ids(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_nested_provider_canonical_ids(child))
    return result


def _normalize_for_hash(value: Any) -> Any:
    """Return the corpus' deterministic JSON normalization.

    The contract intentionally does not depend on platform newlines, mapping
    insertion order, or Unicode composition.  Array order remains semantic.
    """

    if isinstance(value, dict):
        return {key: _normalize_for_hash(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_for_hash(item) for item in value]
    if isinstance(value, str):
        normalized_newlines = value.replace("\r\n", "\n").replace("\r", "\n")
        return unicodedata.normalize("NFC", normalized_newlines)
    return value


def normalized_input_sha256(input_payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _normalize_for_hash(input_payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _expected_provider_receipt_refs(case: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return the legacy ID/missing-path view used by the original G0 report.

    An ID in a case input is only an opaque fixture field.  It is not evidence
    that a Provider call happened.  Static evidence authority is now carried by
    the hash-bound subject receipt registry below.
    """

    if case.get("entry") != "BUILDER":
        return [], []
    payload = case.get("input", {})
    receipt_ids: set[str] = set()
    missing_paths: set[str] = set()

    def add_stop(stop: Any, path: str) -> None:
        if not isinstance(stop, dict):
            return
        receipt_id = stop.get("provider_receipt_id")
        if isinstance(receipt_id, str) and receipt_id:
            receipt_ids.add(receipt_id)
        else:
            missing_paths.add(path)

    add_stop(payload.get("seed"), "input.seed")
    for index, stop in enumerate(payload.get("initial_route", [])):
        add_stop(stop, f"input.initial_route[{index}]")
    for index, candidate in enumerate(payload.get("candidate_snapshot", [])):
        add_stop(candidate, f"input.candidate_snapshot[{index}]")
        for leg_index, leg in enumerate(candidate.get("route_legs", [])):
            receipt_id = leg.get("receipt_id")
            if isinstance(receipt_id, str) and receipt_id:
                receipt_ids.add(receipt_id)
            else:
                missing_paths.add(f"input.candidate_snapshot[{index}].route_legs[{leg_index}]")
    return sorted(receipt_ids), sorted(missing_paths)


def _input_subjects(case: dict[str, Any]) -> list[tuple[str, str, Any, str | None]]:
    """Enumerate every static input subtree which may look Provider-derived.

    Import text itself is user/authored input and therefore is not a Provider
    subject.  Controlled facts are enumerated one top-level fact at a time.
    Builder POIs and route legs are enumerated at stop level so one receipt
    cannot silently stand in for repeated occurrences of the same POI.
    """

    payload = case.get("input", {})
    subjects: list[tuple[str, str, Any, str | None]] = []
    if case.get("entry") == "IMPORT":
        controlled_facts = payload.get("controlled_facts", {})
        if isinstance(controlled_facts, dict):
            for key in sorted(controlled_facts):
                subjects.append(
                    (
                        f"input.controlled_facts.{key}",
                        "IMPORT_CONTROLLED_FACT",
                        controlled_facts[key],
                        None,
                    )
                )
        return subjects

    def add_stop(stop: Any, path: str, subject_type: str) -> None:
        if not isinstance(stop, dict):
            return
        receipt_id = stop.get("provider_receipt_id")
        subjects.append(
            (
                path,
                subject_type,
                stop,
                receipt_id if isinstance(receipt_id, str) and receipt_id else None,
            )
        )

    add_stop(payload.get("seed"), "input.seed", "BUILDER_SEED")
    for index, stop in enumerate(payload.get("initial_route", [])):
        add_stop(stop, f"input.initial_route[{index}]", "BUILDER_INITIAL_STOP")
    for index, candidate in enumerate(payload.get("candidate_snapshot", [])):
        add_stop(candidate, f"input.candidate_snapshot[{index}]", "BUILDER_CANDIDATE")
        if not isinstance(candidate, dict):
            continue
        for leg_index, leg in enumerate(candidate.get("route_legs", [])):
            if not isinstance(leg, dict):
                continue
            receipt_id = leg.get("receipt_id")
            subjects.append(
                (
                    f"input.candidate_snapshot[{index}].route_legs[{leg_index}]",
                    "BUILDER_ROUTE_LEG",
                    leg,
                    receipt_id if isinstance(receipt_id, str) and receipt_id else None,
                )
            )
    return subjects


def expected_subject_receipt_records(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the only truthful static receipt for each current corpus subject.

    Development inputs are explicitly synthetic controlled fixtures.  Frozen
    blind inputs have no repository-visible historical Provider call artifact,
    so their static evidence remains UNAVAILABLE.  Neither classification is a
    POI/current-fact/route-time Provider receipt.
    """

    evidence_class = (
        "UNAVAILABLE" if case.get("split") == "frozen_blind" else "CONTROLLED_FIXTURE_EXECUTION"
    )
    records: list[dict[str, Any]] = []
    for subject_path, subject_type, subject, declared_receipt_id in _input_subjects(case):
        subject_sha256 = _canonical_json_sha256(subject)
        receipt_digest = _canonical_json_sha256(
            {
                "case_id": case["case_id"],
                "subject_path": subject_path,
                "subject_sha256": subject_sha256,
                "evidence_class": evidence_class,
            }
        )
        unavailable = evidence_class == "UNAVAILABLE"
        records.append(
            {
                "schema_version": "dual-entry-subject-evidence-receipt-v1",
                "receipt_id": f"{'unavailable' if unavailable else 'fixture'}-{receipt_digest[:32]}",
                "case_id": case["case_id"],
                "split": case["split"],
                "entry": case["entry"],
                "subject_path": subject_path,
                "subject_type": subject_type,
                "subject_sha256": subject_sha256,
                "case_input_sha256": case["normalized_input_sha256"],
                "evidence_class": evidence_class,
                "execution_mode": "unavailable" if unavailable else "fixture",
                "provider": None if unavailable else "controlled_fixture",
                "provider_call_attempted": False,
                "declared_input_receipt_id": declared_receipt_id,
                "claim_scope": "DATASET_INPUT_BYTES_ONLY",
                "current_fact_authority": False,
                "live_provider_evidence": False,
                "observed_at": None,
                "reason_code": "NO_HISTORICAL_CALL_ARTIFACT" if unavailable else None,
                "source_artifact": None,
            }
        )
    return records


def expected_subject_receipt_refs(case: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "subject_path": record["subject_path"],
            "receipt_id": record["receipt_id"],
            "record_sha256": _canonical_json_sha256(record),
        }
        for record in expected_subject_receipt_records(case)
    ]


def _is_development_split(split: str) -> bool:
    return split in {"pilot", "dev", "regression"}


def validate_dataset() -> dict[str, Any]:
    manifest = _load_json(DATASET_ROOT / "manifest.json")
    case_schema = _load_json(DATASET_ROOT / manifest["schemas"]["case"])
    label_schema = _load_json(DATASET_ROOT / manifest["schemas"]["label"])
    sealed_label_manifest_schema = _load_json(
        DATASET_ROOT / manifest["schemas"]["sealed_label_manifest"]
    )
    source_schema = _load_json(DATASET_ROOT / manifest["schemas"]["source"])
    subject_receipt_schema = _load_json(DATASET_ROOT / manifest["schemas"]["subject_receipt"])
    run_spec_schema = _load_json(DATASET_ROOT / manifest["schemas"]["run_spec"])

    errors: list[str] = []
    warnings: list[str] = []
    all_cases: list[dict[str, Any]] = []
    all_labels: list[dict[str, Any]] = []
    sealed_blind_label_count = 0
    source_rows = _load_jsonl(DATASET_ROOT / manifest["source_registry"])
    source_by_id = {row["source_document_id"]: row for row in source_rows}
    if len(source_by_id) != len(source_rows):
        errors.append("source_registry: duplicate source_document_id")
    for row in source_rows:
        errors.extend(_schema_errors(row, source_schema, f"source:{row.get('source_document_id', '?')}"))
        source_id = row.get("source_document_id", "?")
        raw_hash = row.get("raw_hash")
        extract_hash = row.get("extract_hash")
        raw_path_value = row.get("raw_archive_path")
        extract_path_value = row.get("extract_archive_path")
        if bool(raw_hash) != bool(extract_hash):
            errors.append(f"source:{source_id}: raw_hash and extract_hash must be populated together")
        if bool(raw_path_value) != bool(raw_hash):
            errors.append(f"source:{source_id}: raw_archive_path must be populated exactly when raw_hash is populated")
        if bool(extract_path_value) != bool(extract_hash):
            errors.append(f"source:{source_id}: extract_archive_path must be populated exactly when extract_hash is populated")
        archive_paths: dict[str, Path] = {}
        for kind, path_value, expected_hash in (
            ("raw", raw_path_value, raw_hash),
            ("extract", extract_path_value, extract_hash),
        ):
            if not path_value or not expected_hash:
                continue
            archive_path = (DATASET_ROOT / path_value).resolve()
            try:
                archive_path.relative_to(DATASET_ROOT.resolve())
            except ValueError:
                errors.append(f"source:{source_id}: {kind}_archive_path escapes dataset root")
                continue
            if not archive_path.is_file():
                errors.append(f"source:{source_id}: {kind} archive does not exist: {path_value}")
                continue
            actual_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                errors.append(
                    f"source:{source_id}: {kind} archive hash mismatch: expected {expected_hash}, got {actual_hash}"
                )
            archive_paths[kind] = archive_path
        if "raw" in archive_paths and "extract" in archive_paths:
            try:
                raw_archive = _load_json(archive_paths["raw"])
                extract_archive = _load_json(archive_paths["extract"])
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"source:{source_id}: source archive JSON is unreadable: {exc}")
                continue
            for kind, archive in (("raw", raw_archive), ("extract", extract_archive)):
                if archive.get("source_document_id") != source_id:
                    errors.append(f"source:{source_id}: {kind} archive source_document_id mismatch")
                if archive.get("canonical_url") != row.get("canonical_url"):
                    errors.append(f"source:{source_id}: {kind} archive canonical_url mismatch")
            if extract_archive.get("derivation", {}).get("raw_archive_path") != raw_path_value:
                errors.append(f"source:{source_id}: extract does not trace to declared raw archive")
            raw_body_hash = raw_archive.get("http", {}).get("remote_body_sha256")
            extract_body_hash = extract_archive.get("derivation", {}).get("source_body_sha256")
            if not raw_body_hash or extract_body_hash != raw_body_hash:
                errors.append(f"source:{source_id}: extract source body hash does not trace to raw capture")
            extract_modes = set(extract_archive.get("allowed_use", []))
            registry_modes = set(row.get("usage_modes", []))
            if not extract_modes or not extract_modes <= registry_modes:
                errors.append(f"source:{source_id}: extract allowed_use expands registry usage_modes")
            if row.get("source_type") == "OPEN_DATA":
                provenance_fields = (
                    "license_spdx",
                    "license_url",
                    "attribution",
                    "source_revision",
                    "revision_url",
                    "content_hash",
                    "rights_source_document_id",
                )
                missing = [field for field in provenance_fields if not row.get(field)]
                if missing:
                    errors.append(f"source:{source_id}: archived open source provenance incomplete: {missing}")
                if row.get("content_hash") != raw_body_hash:
                    errors.append(f"source:{source_id}: registry content_hash does not match captured revision body")
                if extract_archive.get("content_sha256") != row.get("content_hash"):
                    errors.append(f"source:{source_id}: extract content_sha256 does not match registry")
                for field in ("source_revision", "revision_url"):
                    if str(raw_archive.get("revision", {}).get(field)) != str(row.get(field)):
                        errors.append(f"source:{source_id}: raw archive {field} does not match registry")
                    if str(extract_archive.get(field)) != str(row.get(field)):
                        errors.append(f"source:{source_id}: extract {field} does not match registry")
                rights_id = row.get("rights_source_document_id")
                if rights_id not in source_by_id:
                    errors.append(f"source:{source_id}: rights source is missing from registry")
            if row.get("source_kind") == "wikivoyage_community":
                required_contributions = {"CONTENT_RELEVANCE", "DIVERSITY", "ROUTE_ADJACENCY"}
                required_prohibitions = {
                    "CURRENT_OPENING",
                    "CURRENT_RESERVATION",
                    "CURRENT_PRICE",
                    "CURRENT_ACCESSIBILITY",
                    "CURRENT_ROUTE_TIME",
                    "CURRENT_POPULARITY",
                    "CANONICAL_IDENTITY",
                    "COORDINATES",
                }
                if "FACT" in registry_modes or "FACT" in extract_modes:
                    errors.append(f"source:{source_id}: Wikivoyage community prior cannot use FACT mode")
                if extract_archive.get("artifact_kind") != "COMMUNITY_ROUTE_PRIOR":
                    errors.append(f"source:{source_id}: community extract artifact_kind mismatch")
                if set(extract_archive.get("allowed_contributions", [])) != required_contributions:
                    errors.append(f"source:{source_id}: community contribution boundary mismatch")
                if not required_prohibitions <= set(extract_archive.get("prohibited_claims", [])):
                    errors.append(f"source:{source_id}: community prohibited claims are incomplete")
                forbidden_keys = {
                    "coordinates",
                    "canonical_place_id",
                    "current_opening",
                    "current_reservation",
                    "current_price",
                    "current_accessibility",
                    "current_route_time",
                    "current_popularity",
                }
                leaked_keys = forbidden_keys & _nested_keys(extract_archive)
                if leaked_keys:
                    errors.append(f"source:{source_id}: community extract contains forbidden fact fields {sorted(leaked_keys)}")

    receipt_registry_value = manifest.get("subject_receipt_registry")
    receipt_registry_path = DATASET_ROOT / str(receipt_registry_value)
    subject_receipt_rows: list[dict[str, Any]] = []
    if not isinstance(receipt_registry_value, str) or not receipt_registry_value:
        errors.append("subject_receipt_registry: manifest path is missing")
    elif not receipt_registry_path.is_file():
        errors.append("subject_receipt_registry: file is missing")
    else:
        actual_registry_sha256 = hashlib.sha256(receipt_registry_path.read_bytes()).hexdigest()
        if manifest.get("subject_receipt_registry_sha256") != actual_registry_sha256:
            errors.append(
                "subject_receipt_registry: file hash mismatch: "
                f"expected {manifest.get('subject_receipt_registry_sha256')}, got {actual_registry_sha256}"
            )
        subject_receipt_rows = _load_jsonl(receipt_registry_path)
    subject_receipt_by_id: dict[str, dict[str, Any]] = {}
    subject_receipt_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in subject_receipt_rows:
        receipt_id = row.get("receipt_id", "?")
        errors.extend(_schema_errors(row, subject_receipt_schema, f"subject_receipt:{receipt_id}"))
        key = (str(row.get("case_id")), str(row.get("subject_path")))
        if receipt_id in subject_receipt_by_id:
            errors.append(f"subject_receipt:{receipt_id}: duplicate receipt_id")
        else:
            subject_receipt_by_id[str(receipt_id)] = row
        if key in subject_receipt_by_key:
            errors.append(f"subject_receipt:{key[0]}:{key[1]}: duplicate case/path binding")
        else:
            subject_receipt_by_key[key] = row

    reference_snapshot_ids: set[str] = set()
    receipt_contract = manifest.get("receipt_evidence_contract", {})
    reference_snapshot = receipt_contract.get("reference_provider_snapshot", {})
    reference_path_value = reference_snapshot.get("path")
    if not isinstance(reference_path_value, str) or not reference_path_value:
        errors.append("receipt_evidence_contract: reference Provider snapshot path is missing")
    else:
        reference_path = (BACKEND_ROOT.parent / reference_path_value).resolve()
        try:
            reference_path.relative_to(BACKEND_ROOT.parent.resolve())
        except ValueError:
            errors.append("receipt_evidence_contract: reference Provider snapshot escapes repository root")
        else:
            if not reference_path.is_file():
                errors.append("receipt_evidence_contract: reference Provider snapshot is missing")
            else:
                reference_bytes = reference_path.read_bytes()
                reference_sha256 = hashlib.sha256(reference_bytes).hexdigest()
                if reference_snapshot.get("sha256") != reference_sha256:
                    errors.append("receipt_evidence_contract: reference Provider snapshot hash mismatch")
                try:
                    reference_payload = json.loads(reference_bytes)
                except json.JSONDecodeError as exc:
                    errors.append(f"receipt_evidence_contract: reference Provider snapshot is invalid JSON: {exc}")
                else:
                    reference_snapshot_ids = _nested_provider_canonical_ids(reference_payload)
        if reference_snapshot.get("identity_field") != "provider_receipt.canonical_place_id":
            errors.append("receipt_evidence_contract: Provider binding must use canonical receipt identity")
        if reference_snapshot.get("binding_status") != "REFERENCE_ONLY_NOT_STATIC_CASE_RECEIPT":
            errors.append("receipt_evidence_contract: reference snapshot must not claim static case binding")

    source_domains_by_split: dict[str, set[str]] = defaultdict(set)
    normalized_hash_cases: dict[str, list[str]] = defaultdict(list)
    legacy_receipt_gap_cases: dict[str, list[str]] = {}
    legacy_gap_classification: Counter[str] = Counter()
    legacy_gap_reference_identity_overlap = 0
    unavailable_subject_cases: dict[str, list[str]] = defaultdict(list)
    expected_subject_receipt_keys: set[tuple[str, str]] = set()
    seen_case_ids: set[str] = set()
    declared_counts: Counter[str] = Counter()
    for file_entry in manifest["files"]:
        split = file_entry["split"]
        inputs_path = DATASET_ROOT / file_entry["inputs"]
        cases = _load_jsonl(inputs_path)
        labels: list[dict[str, Any]] = []
        declared_counts[split] = file_entry["case_count"]
        if len(cases) != file_entry["case_count"]:
            errors.append(f"{split}: manifest count {file_entry['case_count']} != inputs {len(cases)}")
        case_ids = {row.get("case_id") for row in cases}
        if split == "frozen_blind":
            if "labels" in file_entry:
                errors.append("frozen_blind: repository label payload path is prohibited")
            seal_value = file_entry.get("labels_seal")
            seal_path = DATASET_ROOT / str(seal_value)
            if not isinstance(seal_value, str) or not seal_value or "sealed" not in seal_path.parts:
                errors.append("frozen_blind: metadata seal must be under sealed/")
            elif not seal_path.is_file():
                errors.append("frozen_blind: metadata seal is missing")
            else:
                seal_bytes = seal_path.read_bytes()
                seal_hash = hashlib.sha256(seal_bytes).hexdigest()
                if file_entry.get("labels_seal_sha256") != seal_hash:
                    errors.append("frozen_blind: metadata seal hash mismatch")
                try:
                    seal = json.loads(seal_bytes)
                except json.JSONDecodeError as exc:
                    errors.append(f"frozen_blind: metadata seal is invalid JSON: {exc}")
                else:
                    errors.extend(_schema_errors(seal, sealed_label_manifest_schema, "frozen_blind:seal"))
                    expected_case_ids_sha256 = _canonical_json_sha256(sorted(str(case_id) for case_id in case_ids))
                    if (
                        not isinstance(seal, dict)
                        or seal.get("schema_version") != "dual-entry-sealed-label-manifest-v1"
                        or seal.get("split") != "frozen_blind"
                        or seal.get("scoring_payload_present") is not False
                        or seal.get("external_bundle_required") is not True
                        or seal.get("case_count") != len(cases)
                        or seal.get("case_ids_sha256") != expected_case_ids_sha256
                        or "labels" in seal
                        or _nested_keys(seal)
                        & {"deterministic_truth", "metric_oracles", "judge_rubric", "gate_assertions"}
                    ):
                        errors.append("frozen_blind: metadata seal exposes payload or has invalid case bindings")
                    label_commitment = seal.get("labels_canonical_sha256") if isinstance(seal, dict) else None
                    if (
                        not isinstance(label_commitment, str)
                        or len(label_commitment) != 64
                        or any(character not in "0123456789abcdef" for character in label_commitment)
                    ):
                        errors.append("frozen_blind: metadata seal lacks a concrete label commitment")
                    sealed_blind_label_count = len(cases)
        else:
            labels_value = file_entry.get("labels")
            if not isinstance(labels_value, str) or not labels_value:
                errors.append(f"{split}: development label path is missing")
            else:
                labels_path = DATASET_ROOT / labels_value
                labels = _load_jsonl(labels_path)
                if len(labels) != file_entry["case_count"]:
                    errors.append(f"{split}: manifest count {file_entry['case_count']} != labels {len(labels)}")
                label_ids = {row.get("case_id") for row in labels}
                if case_ids != label_ids:
                    errors.append(f"{split}: input/label case IDs differ")
        for row in cases:
            case_id = row.get("case_id", "?")
            errors.extend(_schema_errors(row, case_schema, f"case:{case_id}"))
            if row.get("split") != split:
                errors.append(f"case:{case_id}: split does not match file")
            if case_id in seen_case_ids:
                errors.append(f"case:{case_id}: duplicate global case_id")
            seen_case_ids.add(case_id)
            if any(key in row for key in ("expected", "oracle", "deterministic_truth", "judge_scores")):
                errors.append(f"case:{case_id}: input leaks scoring labels")
            actual_input_hash = normalized_input_sha256(row.get("input", {}))
            declared_input_hash = row.get("normalized_input_sha256")
            if declared_input_hash != actual_input_hash:
                errors.append(
                    f"case:{case_id}: normalized_input_sha256 mismatch: "
                    f"expected {actual_input_hash}, got {declared_input_hash}"
                )
            normalized_hash_cases[actual_input_hash].append(case_id)

            _, legacy_missing_paths = _expected_provider_receipt_refs(row)
            declared_receipts = row.get("receipt_refs", {})
            expected_subject_records = expected_subject_receipt_records(row)
            expected_subject_refs = expected_subject_receipt_refs(row)
            if declared_receipts.get("subject_evidence_refs") != expected_subject_refs:
                errors.append(f"case:{case_id}: subject evidence refs do not match input bytes and registry policy")
            for expected_record in expected_subject_records:
                subject_path = expected_record["subject_path"]
                key = (case_id, subject_path)
                expected_subject_receipt_keys.add(key)
                actual_record = subject_receipt_by_key.get(key)
                if actual_record is None:
                    errors.append(f"case:{case_id}: subject receipt missing for {subject_path}")
                    continue
                if actual_record != expected_record:
                    errors.append(f"case:{case_id}: subject receipt bytes mismatch for {subject_path}")
                if expected_record["evidence_class"] == "UNAVAILABLE":
                    unavailable_subject_cases[case_id].append(subject_path)
            if legacy_missing_paths:
                legacy_receipt_gap_cases[case_id] = legacy_missing_paths
                record_by_path = {record["subject_path"]: record for record in expected_subject_records}
                subject_by_path = {
                    subject_path: subject
                    for subject_path, _, subject, _ in _input_subjects(row)
                }
                for path in legacy_missing_paths:
                    record = record_by_path.get(path)
                    if record is None:
                        errors.append(f"case:{case_id}: legacy receipt gap path is not enumerated: {path}")
                    else:
                        legacy_gap_classification[record["evidence_class"]] += 1
                    subject = subject_by_path.get(path)
                    if isinstance(subject, dict):
                        explicit_identity = subject.get("canonical_place_id")
                        if path.startswith("input.initial_route["):
                            explicit_identity = subject.get("place_id")
                        if explicit_identity in reference_snapshot_ids:
                            legacy_gap_reference_identity_overlap += 1

            expected_source_receipts = []
            for source_ref in row.get("source_document_refs", []):
                source = source_by_id.get(source_ref)
                if source is None:
                    errors.append(f"case:{case_id}: unknown source ref {source_ref}")
                else:
                    source_domains_by_split[split].add(source["domain"])
                    if source.get("raw_hash") and source.get("extract_hash"):
                        expected_source_receipts.append(
                            {
                                "source_document_id": source_ref,
                                "raw_sha256": source["raw_hash"],
                                "extract_sha256": source["extract_hash"],
                            }
                        )
            expected_source_receipts.sort(key=lambda item: item["source_document_id"])
            if declared_receipts.get("source_receipts") != expected_source_receipts:
                errors.append(f"case:{case_id}: source receipt refs do not match registry archive hashes")
            source_refs = sorted(row.get("source_document_refs", []))
            if source_refs:
                expected_source_family = "recorded-source-doc-set-" + hashlib.sha256(
                    "\n".join(source_refs).encode("utf-8")
                ).hexdigest()[:16]
                if row.get("source_family_id") != expected_source_family:
                    errors.append(f"case:{case_id}: source family does not match the referenced document set")
                if row.get("lineage_status", {}).get("source_family") != "RECORDED":
                    errors.append(f"case:{case_id}: hash-bound source document set must be RECORDED")
            elif (
                row.get("source_family_id") != "source-lineage-unavailable"
                or row.get("lineage_status", {}).get("source_family") != "UNAVAILABLE"
            ):
                errors.append(f"case:{case_id}: absent source lineage must use the explicit unavailable sentinel")
            if row.get("data_origin") == "controlled_mutation" and (
                row.get("mutation_parent_case_id") is None
                or row.get("lineage_status", {}).get("mutation_family") != "RECORDED"
            ):
                errors.append(f"case:{case_id}: controlled_mutation requires a recorded parent lineage")
            if row["entry"] == "BUILDER":
                candidates = row["input"]["candidate_snapshot"]
                if len(candidates) not in {0, 4, 5, 6}:
                    errors.append(f"case:{case_id}: builder snapshot must contain 4-6 candidates or explicit zero-result fault")
                candidate_ids = [candidate["id"] for candidate in candidates]
                if len(candidate_ids) != len(set(candidate_ids)):
                    errors.append(f"case:{case_id}: duplicate candidate IDs")
                if not candidates and not (row["input"].get("provider_error") or row["execution"].get("fault_profile")):
                    errors.append(f"case:{case_id}: empty candidate set requires explicit provider failure")
        for row in labels:
            errors.extend(_schema_errors(row, label_schema, f"label:{row.get('case_id', '?')}"))
        all_cases.extend(cases)
        all_labels.extend(labels)

    extra_subject_receipt_keys = sorted(set(subject_receipt_by_key) - expected_subject_receipt_keys)
    for case_id, subject_path in extra_subject_receipt_keys:
        errors.append(f"subject_receipt:{case_id}:{subject_path}: registry record is not bound to a corpus subject")

    case_by_id = {row["case_id"]: row for row in all_cases}
    family_fields = {
        "source_family_id": "source_family",
        "template_family_id": "template_family",
        "generator_family_id": "generator_family",
    }
    unavailable_lineage: dict[str, list[str]] = defaultdict(list)
    for field, status_field in family_fields.items():
        members: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for case in all_cases:
            status = case["lineage_status"][status_field]
            if status == "UNAVAILABLE":
                unavailable_lineage[status_field].append(case["case_id"])
                expected_unavailable_id = f"{status_field.removesuffix('_family')}-lineage-unavailable"
                if case[field] != expected_unavailable_id:
                    errors.append(f"case:{case['case_id']}: unavailable {field} uses a pseudo-family ID")
            else:
                members[case[field]].append(case)
        for family_id, family_cases in members.items():
            splits = {case["split"] for case in family_cases}
            if "frozen_blind" in splits and any(_is_development_split(split) for split in splits):
                errors.append(f"{field}:{family_id}: crosses development/blind boundary {sorted(splits)}")

    mutation_root_cache: dict[str, str] = {}

    def mutation_root(case_id: str, trail: tuple[str, ...] = ()) -> str:
        if case_id in mutation_root_cache:
            return mutation_root_cache[case_id]
        if case_id in trail:
            errors.append(f"mutation_family: cycle detected at {case_id}")
            return case_id
        case = case_by_id[case_id]
        parent_id = case.get("mutation_parent_case_id")
        if parent_id is None:
            if case["lineage_status"]["mutation_family"] == "RECORDED":
                errors.append(f"case:{case_id}: recorded mutation family requires a parent")
            mutation_root_cache[case_id] = case_id
            return case_id
        if case["lineage_status"]["mutation_family"] != "RECORDED":
            errors.append(f"case:{case_id}: mutation parent requires RECORDED lineage status")
        parent = case_by_id.get(parent_id)
        if parent is None:
            errors.append(f"case:{case_id}: unknown mutation_parent_case_id {parent_id}")
            mutation_root_cache[case_id] = case_id
            return case_id
        if parent.get("entry") != case.get("entry"):
            errors.append(f"case:{case_id}: mutation parent entry differs")
        root_id = mutation_root(parent_id, (*trail, case_id))
        mutation_root_cache[case_id] = root_id
        return root_id

    mutation_families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in all_cases:
        mutation_families[mutation_root(case["case_id"])].append(case)
    for root_id, family_cases in mutation_families.items():
        splits = {case["split"] for case in family_cases}
        if "frozen_blind" in splits and any(_is_development_split(split) for split in splits):
            errors.append(f"mutation_family:{root_id}: crosses development/blind boundary {sorted(splits)}")

    for digest, case_ids in normalized_hash_cases.items():
        if len(case_ids) > 1:
            errors.append(f"normalized_input_sha256:{digest}: duplicate cases {sorted(case_ids)}")

    development_domains = set().union(
        *(source_domains_by_split[split] for split in ("pilot", "dev", "regression"))
    )
    dev_blind_domain_overlap = development_domains & source_domains_by_split["frozen_blind"]
    if dev_blind_domain_overlap:
        errors.append(f"source domains cross development/blind: {sorted(dev_blind_domain_overlap)}")

    labels_by_id = {row["case_id"]: row for row in all_labels}
    metric_names = (
        "parse_f1",
        "entity_precision_recall",
        "finding_precision_recall",
        "repair_postcheck",
        "builder_ndcg_at_5",
        "builder_recall_at_5",
    )
    metric_coverage: dict[str, Counter[str]] = {name: Counter() for name in metric_names}
    for label in all_labels:
        case_id = label["case_id"]
        truth = label["deterministic_truth"]
        metrics = label.get("metric_oracles", {})
        for metric_name in metric_names:
            applicability = metrics.get(metric_name, {}).get("applicability", "MISSING")
            metric_coverage[metric_name][applicability] += 1

        parse_truth = truth.get("expected_parse")
        parse_metric = metrics.get("parse_f1", {})
        if parse_truth is not None and "stop_names" in parse_truth:
            expected_items = [{"stop_name": name} for name in parse_truth["stop_names"]]
            if parse_metric.get("ground_truth_items") != expected_items:
                errors.append(f"label:{case_id}: parse_f1 truth diverges from expected_parse")
        elif parse_metric != {"applicability": "N_A", "reason_code": "NO_STRUCTURED_PARSE_TRUTH"}:
            errors.append(f"label:{case_id}: parse_f1 must be explicit N_A without structured parse truth")

        resolutions = truth.get("expected_resolutions")
        entity_metric = metrics.get("entity_precision_recall", {})
        if resolutions:
            expected_items = [
                {
                    "raw_name": item["raw_name"],
                    "status": item["status"],
                    "canonical_place_id": item.get("canonical_place_id"),
                }
                for item in resolutions
            ]
            if entity_metric.get("ground_truth_items") != expected_items:
                errors.append(f"label:{case_id}: entity metric truth diverges from expected_resolutions")
        elif entity_metric != {"applicability": "N_A", "reason_code": "NO_STRUCTURED_ENTITY_TRUTH"}:
            errors.append(f"label:{case_id}: entity precision/recall must be explicit N_A without entity truth")

        findings = truth.get("expected_findings")
        finding_metric = metrics.get("finding_precision_recall", {})
        if findings:
            scoped_findings = findings
            if finding_metric.get("metric_version") == "exact-set-blocker-high-v1":
                if finding_metric.get("scope_severities") != ["BLOCKER", "HIGH"]:
                    errors.append(f"label:{case_id}: blocker/high finding scope is not explicit")
                scoped_findings = [
                    item for item in findings if item.get("severity") in {"BLOCKER", "HIGH"}
                ]
            expected_items = [
                {
                    "reason_code": item["reason_code"],
                    "status": item["status"],
                    "subject": item.get("subject"),
                    "affected_member": item.get("affected_member"),
                }
                for item in scoped_findings
            ]
            if finding_metric.get("ground_truth_items") != expected_items:
                errors.append(f"label:{case_id}: finding metric truth diverges from expected_findings")
        elif finding_metric != {"applicability": "N_A", "reason_code": "NO_STRUCTURED_FINDING_TRUTH"}:
            errors.append(f"label:{case_id}: finding precision/recall must be explicit N_A without finding truth")

        repair = truth.get("repair_oracle")
        repair_metric = metrics.get("repair_postcheck", {})
        if repair:
            expected_predicates = [
                {"predicate": "postcheck_executed", "expected": repair.get("postcheck_required", False)},
                {"predicate": "locked_items_preserved", "expected": repair.get("locked_items_preserved", False)},
                {"predicate": "no_new_hard_violation", "expected": repair.get("no_new_hard_violation", False)},
            ]
            if (
                repair_metric.get("max_options") != repair["max_options"]
                or repair_metric.get("allowed_operation_types") != repair.get("allowed_operation_types", [])
                or repair_metric.get("required_predicates") != expected_predicates
            ):
                errors.append(f"label:{case_id}: repair/postcheck metric diverges from repair_oracle")
        elif repair_metric != {"applicability": "N_A", "reason_code": "NO_STRUCTURED_REPAIR_TRUTH"}:
            errors.append(f"label:{case_id}: repair/postcheck must be explicit N_A without repair truth")

        order = truth.get("expected_candidate_order")
        ndcg_metric = metrics.get("builder_ndcg_at_5", {})
        if order:
            expected_relevance = [
                {"candidate_id": candidate_id, "relevance_grade": max(5 - index, 0)}
                for index, candidate_id in enumerate(order)
            ]
            if ndcg_metric.get("relevance_items") != expected_relevance:
                errors.append(f"label:{case_id}: Builder nDCG truth diverges from expected order")
        elif ndcg_metric != {"applicability": "N_A", "reason_code": "NO_GRADED_RANKING_TRUTH"}:
            errors.append(f"label:{case_id}: Builder nDCG must be explicit N_A without graded order")

        acceptable = truth.get("acceptable_candidate_ids")
        recall_metric = metrics.get("builder_recall_at_5", {})
        if acceptable:
            if recall_metric.get("relevant_candidate_ids") != acceptable:
                errors.append(f"label:{case_id}: Builder Recall@5 truth diverges from acceptable IDs")
        elif recall_metric != {"applicability": "N_A", "reason_code": "NO_RELEVANT_CANDIDATE_TRUTH"}:
            errors.append(f"label:{case_id}: Builder Recall@5 must be explicit N_A without relevant IDs")

    builder_cases = [row for row in all_cases if row["entry"] == "BUILDER"]
    development_builders = [row for row in builder_cases if row["split"] in {"dev", "regression"}]
    development_builder_tags = {tag for row in development_builders for tag in row["tags"]}
    required_builder_tags = {
        "anchor-query",
        "insert-edge",
        "candidate-6",
        "multi-intent",
        "diversity",
        "wrong-city",
        "wrong-category",
        "duplicate",
        "hard-leak",
        "route-tiers",
        "unknown",
        "far-other-day",
        "frozen-set",
        "expired",
        "stale",
        "cross-workspace",
        "idempotency",
        "replay",
        "reuse-conflict",
        "accept-rollback",
        "four-stop",
        "new-anchor",
        "event-chain",
        "drag",
        "button-equivalence",
        "undo",
        "restart",
    }
    missing_builder_tags = sorted(required_builder_tags - development_builder_tags)
    if missing_builder_tags:
        errors.append(f"builder development contract missing scenario tags: {missing_builder_tags}")

    p5_contract_cases = [row for row in development_builders if "p5-contract" in row["tags"]]
    p5_by_city = Counter(row["city"] for row in p5_contract_cases)
    if dict(p5_by_city) != {"北京": 4, "上海": 4, "杭州": 4}:
        errors.append(f"builder P5 contract cases must be 4 per city, got {dict(p5_by_city)}")
    g2_seeds = [row for row in development_builders if "g2-seed" in row["tags"]]
    g5_seeds = [row for row in development_builders if "g5-seed" in row["tags"]]
    if Counter(row["city"] for row in g2_seeds) != Counter({"北京": 1, "上海": 1, "杭州": 1}):
        errors.append("builder G2 seed contract requires one four-stop snapshot session per city")
    if Counter(row["city"] for row in g5_seeds) != Counter({"北京": 2, "上海": 2, "杭州": 2}):
        errors.append("builder G5 seed contract requires two recovery/browser seeds per city")

    for case in development_builders:
        case_id = case["case_id"]
        tags = set(case["tags"])
        input_payload = case["input"]
        truth = labels_by_id[case_id]["deterministic_truth"]
        if tags & {"anchor-query", "insert-edge"}:
            if "request_context" not in input_payload or "query_oracle" not in truth:
                errors.append(f"case:{case_id}: query-mode case requires request_context and query_oracle")
        if "p5-contract" in tags and "frozen-set" in tags:
            if "suggestion_set_fixture" not in input_payload or "suggestion_set_oracle" not in truth:
                errors.append(f"case:{case_id}: frozen-set contract requires fixture and oracle")
        if "p5-contract" in tags and "idempotency" in tags:
            if "accept_attempts" not in input_payload or "acceptance_oracle" not in truth:
                errors.append(f"case:{case_id}: idempotency contract requires attempts and acceptance oracle")
        if "p5-contract" in tags and "accept-rollback" in tags:
            acceptance = truth.get("acceptance_oracle", {})
            if not input_payload.get("fault_injection") or not acceptance.get("rollback_fault_points"):
                errors.append(f"case:{case_id}: rollback contract requires injected fault points and rollback oracle")
        if "g2-seed" in tags:
            accepted_steps = [step for step in input_payload.get("action_sequence", []) if step.startswith("accept:")]
            if len(accepted_steps) < 3 or "event_oracle" not in truth:
                errors.append(f"case:{case_id}: G2 seed must accept three candidates and define event oracle")
        if "g5-seed" in tags and "recovery_oracle" not in truth:
            errors.append(f"case:{case_id}: G5 seed requires recovery oracle")
        if "button-equivalence" in tags:
            if not input_payload.get("ui_command_pairs") or "interaction_oracle" not in truth:
                errors.append(f"case:{case_id}: drag/button case requires command pair and interaction oracle")

    candidate_sequences: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for case in builder_cases:
        sequence = tuple(
            candidate.get("canonical_place_id", candidate["id"])
            for candidate in case["input"]["candidate_snapshot"]
        )
        if sequence:
            candidate_sequences[sequence].append(case["case_id"])
    for sequence, case_ids in candidate_sequences.items():
        if len(case_ids) > 1:
            errors.append(
                "builder canonical candidate sequence is duplicated: "
                f"{list(sequence)} in {sorted(case_ids)}"
            )

    city_counts = Counter(row["city"] for row in all_cases)
    entry_counts = Counter(row["entry"] for row in all_cases)
    split_counts = Counter(row["split"] for row in all_cases)
    if dict(city_counts) != manifest["current_counts"]["by_city"]:
        errors.append(f"manifest city counts mismatch: {dict(city_counts)}")
    if dict(entry_counts) != manifest["current_counts"]["by_entry"]:
        errors.append(f"manifest entry counts mismatch: {dict(entry_counts)}")
    if dict(split_counts) != manifest["current_counts"]["by_split"]:
        errors.append(f"manifest split counts mismatch: {dict(split_counts)}")
    if len(all_cases) != manifest["current_counts"]["total"]:
        errors.append("manifest total count mismatch")

    run_specs = []
    for path in sorted(RUN_SPEC_ROOT.glob("dual-entry-*.json")):
        spec = _load_json(path)
        errors.extend(_schema_errors(spec, run_spec_schema, f"run_spec:{path.name}"))
        run_specs.append(spec)
    expected_lanes = {"pr_offline", "nightly_snapshot", "weekly_live", "release_blind"}
    if {spec.get("lane") for spec in run_specs} != expected_lanes:
        errors.append("run_specs: expected exactly pr_offline/nightly_snapshot/weekly_live/release_blind")
    runtime_receipt_policy = manifest.get("runtime_receipt_policy", {})
    required_runtime_subjects = {
        "IMPORT_OFFERED_CANDIDATE",
        "IMPORT_REJECTED_CANDIDATE",
        "IMPORT_MATERIALIZED_PLACE",
        "BUILDER_CANDIDATE",
        "BUILDER_ROUTE_LEG",
        "BUILDER_CURRENT_FACT",
    }
    if set(runtime_receipt_policy.get("required_subjects", [])) != required_runtime_subjects:
        errors.append("runtime_receipt_policy: required Provider subject set is incomplete")
    if runtime_receipt_policy.get("static_fixture_substitution_allowed") is not False:
        errors.append("runtime_receipt_policy: static fixtures must not substitute for runtime Provider receipts")
    if runtime_receipt_policy.get("source_prior_substitution_allowed") is not False:
        errors.append("runtime_receipt_policy: source priors must not substitute for runtime Provider receipts")
    for spec in run_specs:
        if spec.get("provider", {}).get("receipts_required") is True and "provider_receipts.jsonl" not in spec.get(
            "artifacts", []
        ):
            errors.append(f"run_spec:{spec.get('lane')}: receipts_required lacks provider_receipts.jsonl artifact")
    release_specs = [spec for spec in run_specs if spec.get("lane") == "release_blind"]
    if not release_specs or release_specs[0]["dataset"]["label_access"] != "isolated_scorer_only":
        errors.append("release_blind: label_access must be isolated_scorer_only")
    nightly_specs = [spec for spec in run_specs if spec.get("lane") == "nightly_snapshot"]
    expected_builder_seed_groups = {"G2_FOUR_STOP_BUILDER", "G5_BROWSER_RECOVERY"}
    if not nightly_specs:
        errors.append("nightly_snapshot: missing Builder gate seed contract")
    else:
        gate_seeds = nightly_specs[0]["dataset"].get("gate_case_seeds", {})
        if set(gate_seeds) != expected_builder_seed_groups:
            errors.append(
                "nightly_snapshot: expected exactly G2_FOUR_STOP_BUILDER and G5_BROWSER_RECOVERY seed groups"
            )
        for group_name, group in gate_seeds.items():
            unknown_ids = sorted(set(group["case_ids"]) - seen_case_ids)
            if unknown_ids:
                errors.append(f"nightly_snapshot:{group_name}: unknown case IDs {unknown_ids}")
            blind_ids = sorted(
                case_id
                for case_id in group["case_ids"]
                if next((row["split"] for row in all_cases if row["case_id"] == case_id), None) == "frozen_blind"
            )
            if blind_ids:
                errors.append(f"nightly_snapshot:{group_name}: development seed list references blind cases {blind_ids}")
            if len(group["case_ids"]) < group["required_min"]:
                warnings.append(
                    f"{group_name} seeds {len(group['case_ids'])}/{group['required_min']} "
                    f"({group['execution_status']})"
                )

    blind_cases = [row for row in all_cases if row["split"] == "frozen_blind"]
    blind_import = sum(row["entry"] == "IMPORT" for row in blind_cases)
    blind_builder = sum(row["entry"] == "BUILDER" for row in blind_cases)
    blind_fault = sum(bool(row["execution"].get("fault_profile")) or "restart" in row["tags"] or "concurrency" in row["tags"] for row in blind_cases)
    minimums = manifest["final_gate_minimums"]
    release_blockers = list(manifest["release_blockers"])
    if blind_import < minimums["frozen_blind_import"]:
        warnings.append(f"release corpus: import {blind_import}/{minimums['frozen_blind_import']}")
    if blind_builder < minimums["frozen_blind_builder_suggestion"]:
        warnings.append(f"release corpus: builder {blind_builder}/{minimums['frozen_blind_builder_suggestion']}")
    if blind_fault < minimums["frozen_blind_fault_or_recovery"]:
        warnings.append(f"release corpus: fault/recovery {blind_fault}/{minimums['frozen_blind_fault_or_recovery']}")
    used_source_ids = {ref for row in all_cases for ref in row.get("source_document_refs", [])}
    unhashed_sources = sorted(source_id for source_id in used_source_ids if not source_by_id[source_id].get("raw_hash"))
    if unhashed_sources:
        warnings.append(f"G0 source ingestion pending: {', '.join(unhashed_sources)}")
    if unavailable_lineage:
        warnings.append(
            "family lineage unavailable: "
            + ", ".join(
                f"{family_name}={len(case_ids)}"
                for family_name, case_ids in sorted(unavailable_lineage.items())
            )
        )
    subject_evidence_counts = Counter(row.get("evidence_class") for row in subject_receipt_rows)
    legacy_gap_total = sum(len(paths) for paths in legacy_receipt_gap_cases.values())
    declared_legacy_baseline = manifest.get("receipt_evidence_contract", {}).get(
        "legacy_unbound_builder_subjects"
    )
    if declared_legacy_baseline != legacy_gap_total:
        errors.append(
            "receipt_evidence_contract: legacy gap baseline mismatch: "
            f"expected {legacy_gap_total}, got {declared_legacy_baseline}"
        )
    if subject_evidence_counts.get("UNAVAILABLE", 0):
        unavailable_count = subject_evidence_counts["UNAVAILABLE"]
        warnings.append(
            "static subject evidence unavailable: "
            f"{unavailable_count} subjects in {len(unavailable_subject_cases)} frozen-blind cases; "
            "external Provider call artifacts are required"
        )
    if legacy_gap_classification.get("UNAVAILABLE", 0):
        warnings.append(
            "legacy Provider receipt gaps remaining unavailable: "
            f"{legacy_gap_classification['UNAVAILABLE']}/{legacy_gap_total}; "
            f"{legacy_gap_classification['CONTROLLED_FIXTURE_EXECUTION']} are explicitly controlled fixtures"
        )
    import_rejected_candidate_cases = {
        label["case_id"]
        for label in all_labels
        if "wrong-city" in case_by_id[label["case_id"]].get("tags", [])
        and any(
            item.get("status") == "NOT_FOUND"
            for item in label.get("deterministic_truth", {}).get(
                "expected_resolutions",
                [],
            )
        )
    }
    human_ground_truth_cases = [
        row
        for row in all_cases
        if row["data_origin"] in {"authorized_user", "human_calibration"}
    ]
    release_ready = not errors and not warnings and manifest["release_eligible"]

    return {
        "schema_version": "dual-entry-testset-validation-v1",
        "structurally_valid": not errors,
        "release_ready": release_ready,
        "case_count": len(all_cases),
        "label_count": len(all_labels) + sealed_blind_label_count,
        "development_label_count": len(all_labels),
        "sealed_blind_label_count": sealed_blind_label_count,
        "city_counts": dict(city_counts),
        "entry_counts": dict(entry_counts),
        "split_counts": dict(split_counts),
        "run_spec_lanes": sorted(spec["lane"] for spec in run_specs),
        "pollution_contract": {
            "normalized_hash_duplicates": sum(
                len(case_ids) for case_ids in normalized_hash_cases.values() if len(case_ids) > 1
            ),
            "legacy_unbound_builder_subjects_before": legacy_gap_total,
            "legacy_gap_classification": {
                "real_amap_snapshot_exact_identity_overlap": legacy_gap_reference_identity_overlap,
                "real_amap_snapshot_exact_receipt_binding": subject_evidence_counts["AMAP_FROZEN_SNAPSHOT"],
                "controlled_fixture_execution": legacy_gap_classification[
                    "CONTROLLED_FIXTURE_EXECUTION"
                ],
                "unavailable_no_historical_call": legacy_gap_classification["UNAVAILABLE"],
            },
            "static_subject_registry_records": len(subject_receipt_rows),
            "static_subject_evidence": {
                "real_provider": 0,
                "controlled_fixture_execution": subject_evidence_counts[
                    "CONTROLLED_FIXTURE_EXECUTION"
                ],
                "unavailable_no_historical_call": subject_evidence_counts["UNAVAILABLE"],
            },
            "unavailable_subject_cases": len(unavailable_subject_cases),
            "import_not_found_cases_requiring_runtime_rejected_candidate_receipts": len(
                import_rejected_candidate_cases
            ),
            "unavailable_family_lineage": {
                family_name: len(case_ids)
                for family_name, case_ids in sorted(unavailable_lineage.items())
            },
        },
        "metric_oracle_coverage": {
            name: {
                "applicable": counts["APPLICABLE"],
                "not_applicable": counts["N_A"],
            }
            for name, counts in metric_coverage.items()
        },
        "human_ground_truth_case_count": len(human_ground_truth_cases),
        "builder_contract_coverage": {
            "development_builder_cases": len(development_builders),
            "p5_contract_cases": len(p5_contract_cases),
            "p5_contract_by_city": dict(p5_by_city),
            "g2_four_stop_seeds": len(g2_seeds),
            "g5_recovery_seeds": len(g5_seeds),
            "required_scenario_tags": sorted(required_builder_tags),
            "missing_scenario_tags": missing_builder_tags,
        },
        "errors": errors,
        "warnings": warnings,
        "declared_release_blockers": release_blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args()
    report = validate_dataset()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["structurally_valid"]:
        return 1
    if args.require_release_ready and not report["release_ready"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
