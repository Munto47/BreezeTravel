from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evals.trip_text_cards_v1.contracts import (
    AdjudicationBundle,
    AnnotationBundle,
    CaseAnnotation,
    TextCardInputCase,
    canonical_sha256,
    validate_case_annotation,
)


class AnnotationValidationError(ValueError):
    pass


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _read_model(path: Path, model_type):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return model_type.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise AnnotationValidationError(f"invalid annotation artifact {path.name}: {exc}") from exc


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_semantics(case: CaseAnnotation) -> dict[str, Any]:
    mentions = []
    for mention in sorted(case.mentions, key=lambda item: (item.span_start, item.span_end, item.role)):
        value = mention.model_dump(mode="json")
        value.pop("mention_id", None)
        mentions.append(value)
    return {
        "case_id": case.case_id,
        "source_sha256": case.source_sha256,
        "destination_name": case.destination_name,
        "mentions": mentions,
    }


def _conflict_sha256(left: CaseAnnotation, right: CaseAnnotation) -> str:
    pair = sorted(
        (_case_semantics(left), _case_semantics(right)),
        key=canonical_sha256,
    )
    return canonical_sha256({"case_id": left.case_id, "independent_annotations": pair})


def validate_annotation_bundle(
    path: Path,
    *,
    split: str,
    source_cases: list[TextCardInputCase],
    repository_root: Path,
) -> AnnotationBundle:
    resolved = path.resolve(strict=True)
    if _is_within(resolved, repository_root):
        raise AnnotationValidationError("human annotation bundles must remain outside the repository")
    bundle = _read_model(resolved, AnnotationBundle)
    if bundle.split != split:
        raise AnnotationValidationError("annotation bundle split mismatch")
    expected_ids = [case.case_id for case in source_cases]
    if [case.case_id for case in bundle.cases] != expected_ids:
        raise AnnotationValidationError("annotation bundle must cover the split exactly and in source order")
    source_by_id = {case.case_id: case for case in source_cases}
    for case in bundle.cases:
        try:
            validate_case_annotation(case, source_by_id[case.case_id])
        except ValueError as exc:
            raise AnnotationValidationError(str(exc)) from exc
    return bundle


def verify_adjudication(
    *,
    split: str,
    source_cases: list[TextCardInputCase],
    first_path: Path,
    second_path: Path,
    adjudication_path: Path,
    repository_root: Path,
) -> tuple[AdjudicationBundle, dict[str, Any]]:
    first = validate_annotation_bundle(
        first_path,
        split=split,
        source_cases=source_cases,
        repository_root=repository_root,
    )
    second = validate_annotation_bundle(
        second_path,
        split=split,
        source_cases=source_cases,
        repository_root=repository_root,
    )
    if first.assignment_id == second.assignment_id:
        raise AnnotationValidationError("independent annotations require distinct assignment IDs")
    if first.attestation.actor_id == second.attestation.actor_id:
        raise AnnotationValidationError("independent annotations require distinct human actors")

    resolved_adjudication = adjudication_path.resolve(strict=True)
    if _is_within(resolved_adjudication, repository_root):
        raise AnnotationValidationError("adjudication bundles must remain outside the repository")
    adjudication = _read_model(resolved_adjudication, AdjudicationBundle)
    if adjudication.split != split:
        raise AnnotationValidationError("adjudication split mismatch")
    if set(adjudication.source_assignment_ids) != {first.assignment_id, second.assignment_id}:
        raise AnnotationValidationError("adjudication assignment binding mismatch")
    if set(adjudication.source_bundle_sha256) != {
        _file_sha256(first_path),
        _file_sha256(second_path),
    }:
        raise AnnotationValidationError("adjudication source byte binding mismatch")
    if adjudication.attestation.actor_id in {
        first.attestation.actor_id,
        second.attestation.actor_id,
    }:
        raise AnnotationValidationError("adjudicator must be distinct from both annotators")

    expected_ids = [case.case_id for case in source_cases]
    if [case.case_id for case in adjudication.gold_cases] != expected_ids:
        raise AnnotationValidationError("adjudicated gold must cover the split exactly and in source order")
    source_by_id = {case.case_id: case for case in source_cases}
    first_by_id = {case.case_id: case for case in first.cases}
    second_by_id = {case.case_id: case for case in second.cases}
    gold_by_id = {case.case_id: case for case in adjudication.gold_cases}
    for case in adjudication.gold_cases:
        try:
            validate_case_annotation(case, source_by_id[case.case_id])
        except ValueError as exc:
            raise AnnotationValidationError(str(exc)) from exc

    expected_conflicts: dict[str, str] = {}
    for case_id in expected_ids:
        left = first_by_id[case_id]
        right = second_by_id[case_id]
        if _case_semantics(left) == _case_semantics(right):
            if _case_semantics(gold_by_id[case_id]) != _case_semantics(left):
                raise AnnotationValidationError(f"{case_id} agreed truth changed during adjudication")
        else:
            expected_conflicts[case_id] = _conflict_sha256(left, right)
    actual_conflicts = {item.case_id: item.conflict_sha256 for item in adjudication.conflicts}
    if actual_conflicts != expected_conflicts:
        raise AnnotationValidationError("adjudication conflict set or fingerprint mismatch")

    executable_mentions = sum(
        mention.executable_place
        for case in adjudication.gold_cases
        for mention in case.mentions
    )
    canonical_executable_mentions = sum(
        mention.executable_place and mention.canonical_place is not None
        for case in adjudication.gold_cases
        for mention in case.mentions
    )
    minimum_met = split == "dev" or executable_mentions >= 65
    if not minimum_met:
        raise AnnotationValidationError(f"{split} requires at least 65 gold executable mentions")
    if canonical_executable_mentions != executable_mentions:
        raise AnnotationValidationError("every executable gold mention requires human-verified canonical truth")

    receipt = {
        "schema_version": "g01-text-card-annotation-verification-receipt-v1",
        "split": split,
        "case_count": len(source_cases),
        "annotator_count": 2,
        "adjudicator_count": 1,
        "actors_distinct": True,
        "independent_attestations_valid": True,
        "conflict_count": len(expected_conflicts),
        "conflicts_adjudicated": len(expected_conflicts),
        "evidence_span_validity": 1.0,
        "gold_executable_mentions": executable_mentions,
        "canonical_gold_executable_mentions": canonical_executable_mentions,
        "minimum_gold_denominator_met": minimum_met,
        "human_evidence_level": "DUAL_HUMAN_ADJUDICATED",
    }
    return adjudication, receipt


def build_blank_work_packet(
    *,
    split: str,
    assignment_id: str,
    source_cases: list[TextCardInputCase],
) -> dict[str, Any]:
    return {
        "schema_version": "g01-text-card-annotation-work-packet-v1",
        "dataset_version": "g01-text-card-dataset-v1",
        "assignment_id": assignment_id,
        "split": split,
        "status": "BLANK_HUMAN_WORK_PACKET",
        "instructions": [
            "Annotate independently without model or peer suggestions.",
            "Use Unicode code-point half-open spans against input_text.",
            "Only atomic PLANNED place mentions with a day are executable.",
            "Verify every executable canonical place against an admitted provider receipt.",
            "Submit a separate g01-text-card-annotation-bundle-v1 artifact.",
        ],
        "cases": [
            {
                "case_id": case.case_id,
                "source_sha256": case.normalized_input_sha256,
                "input_text": case.input_text,
                "annotation": None,
            }
            for case in source_cases
        ],
        "automated_labels_included": False,
        "peer_labels_included": False,
    }
