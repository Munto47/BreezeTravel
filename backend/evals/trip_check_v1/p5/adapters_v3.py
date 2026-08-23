"""P5 v3 eval adapters over receipt-bound entity-resolution materializations."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.data_contract_v2 import _fault_artifact
from evals.trip_check_v1.p5.data_contract_v3 import (
    evidence_projection_v3,
    ocr_source_binding_projection_v3,
)
from evals.trip_check_v1.p5.evidence_materialization_v3 import (
    EVIDENCE_MATERIALIZATION_SCHEMA_V3,
    validate_evidence_materialization_v3,
)


def _artifact_binding_v3(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact.get("artifact_id"),
        "schema_version": artifact.get("schema_version"),
        "content_sha256": artifact.get("content_sha256"),
    }


def _receipt_binding_v3(
    receipt: Mapping[str, Any] | None, *, artifact_id: str
) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "artifact_id": artifact_id,
        "schema_version": receipt.get("schema_version"),
        "content_sha256": digest(receipt),
    }


def validate_materialization_v3(
    case: P5CaseV3, value: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate the complete outer envelope and its deterministic v3 evidence."""

    materialization = deepcopy(dict(value))
    expected_fields = {
        "schema_version",
        "materialization_id",
        "case_id",
        "source_payload",
        "render_receipt",
        "ocr_baseline_receipt",
        "cleanup_receipt",
        "ocr_source_binding",
        "provider_snapshot",
        "evidence_snapshot",
        "candidate_sets",
        "fault_script",
        "receipts",
        "evidence_materialization_hash",
        "materialization_hash",
    }
    if set(materialization) != expected_fields:
        raise ValueError("P5 v3 outer materialization fields mismatch")
    if materialization.get("schema_version") != "trip-check-p5-materialization-v3":
        raise ValueError("unsupported P5 v3 outer materialization schema")
    if materialization.get("case_id") != case.case_id:
        raise ValueError("P5 v3 case/materialization ID mismatch")
    actual_hash = materialization.pop("materialization_hash")
    if actual_hash != digest(materialization):
        raise ValueError("P5 v3 outer materialization hash mismatch")
    materialization["materialization_hash"] = actual_hash
    if (
        case.materialization.materialization_id != materialization["materialization_id"]
        or case.materialization.materialization_sha256 != actual_hash
    ):
        raise ValueError("P5 v3 outer materialization binding mismatch")

    inner = evidence_projection_v3(materialization)
    expected_source_binding = ocr_source_binding_projection_v3(inner)
    if materialization["ocr_source_binding"] != expected_source_binding:
        raise ValueError("P5 v3 outer OCR source binding differs from inner evidence")
    if case.materialization.ocr_source_binding is None:
        if materialization["ocr_source_binding"] is not None:
            raise ValueError("P5 v3 text case carries OCR source provenance")
    elif case.materialization.ocr_source_binding.model_dump(mode="json") != expected_source_binding:
        raise ValueError("P5 v3 case OCR source binding mismatch")

    artifact_pairs = (
        (case.materialization.source_payload, materialization["source_payload"]),
        (case.materialization.provider_snapshot, materialization["provider_snapshot"]),
        (case.materialization.evidence_snapshot, materialization["evidence_snapshot"]),
        (case.materialization.fault_script, materialization["fault_script"]),
    )
    for expected, artifact in artifact_pairs:
        if not isinstance(artifact, Mapping) or expected.model_dump(mode="json") != _artifact_binding_v3(
            artifact
        ):
            raise ValueError("P5 v3 materialization artifact binding mismatch")
    candidates = materialization["candidate_sets"]
    if not isinstance(candidates, list) or any(
        not isinstance(item, Mapping) for item in candidates
    ):
        raise ValueError("P5 v3 candidate sets are invalid")
    if [item.model_dump(mode="json") for item in case.materialization.candidate_sets] != [
        _artifact_binding_v3(item) for item in candidates
    ]:
        raise ValueError("P5 v3 CandidateSet bindings mismatch")

    receipt_pairs = (
        (
            case.materialization.render_receipt,
            materialization["render_receipt"],
            f"render-{case.case_id}",
        ),
        (
            case.materialization.ocr_baseline_receipt,
            materialization["ocr_baseline_receipt"],
            f"ocr-{case.case_id}",
        ),
        (
            case.materialization.cleanup_receipt,
            materialization["cleanup_receipt"],
            f"cleanup-{case.case_id}",
        ),
    )
    for expected, receipt, artifact_id in receipt_pairs:
        actual = _receipt_binding_v3(receipt, artifact_id=artifact_id)
        if (expected.model_dump(mode="json") if expected is not None else None) != actual:
            raise ValueError("P5 v3 historical screenshot receipt binding mismatch")

    expected_fault = _fault_artifact(case.model_dump(mode="json", exclude_none=True))
    if materialization["fault_script"] != expected_fault:
        raise ValueError("P5 v3 fault artifact differs from deterministic registry output")
    return materialization


class MaterializedResolutionProviderV3:
    """Replay place.search candidates exactly as bound by v3 resolution receipts."""

    def __init__(self, materialization: Mapping[str, Any]):
        if materialization.get("schema_version") != EVIDENCE_MATERIALIZATION_SCHEMA_V3:
            raise ValueError("P5 v3 resolution provider requires validated inner evidence")
        materialization = validate_evidence_materialization_v3(materialization)
        source = materialization.get("source_payload")
        receipts = materialization.get("receipts")
        if not isinstance(source, Mapping) or not isinstance(receipts, list):
            raise ValueError("P5 v3 resolution provider materialization is incomplete")
        receipt_by_id = {
            str(item.get("receipt_id")): item
            for item in receipts
            if isinstance(item, Mapping) and item.get("operation") == "place.search"
        }
        resolutions = source.get("entity_resolutions")
        if not isinstance(resolutions, list):
            raise ValueError("P5 v3 resolution provider has no entity resolutions")
        self._responses: dict[tuple[str, str], list[dict[str, Any]]] = {}
        target_city = str(source.get("city"))
        for resolution in resolutions:
            if not isinstance(resolution, Mapping):
                raise ValueError("P5 v3 entity resolution entry is invalid")
            raw_name = str(resolution.get("raw_name"))
            receipt = receipt_by_id.get(str(resolution.get("search_receipt_id")))
            candidates = resolution.get("candidates")
            if receipt is None or not isinstance(candidates, list):
                raise ValueError("P5 v3 entity resolution receipt binding is incomplete")
            rows: list[dict[str, Any]] = []
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    raise ValueError("P5 v3 entity resolution candidate is invalid")
                rows.append(
                    {
                        **dict(candidate),
                        "provider_place_id": candidate.get("place_id"),
                        "retrieval_provider": receipt.get("provider"),
                        "execution_mode": receipt.get("execution_mode"),
                        "retrieval_request_hash": receipt.get("request_hash"),
                        "retrieval_response_hash": receipt.get("response_hash"),
                        "retrieval_observed_at": receipt.get("observed_at"),
                        "source_url": receipt.get("source_url"),
                        "opening_hours": "07:00-22:00",
                    }
                )
            key = (raw_name, target_city)
            existing = self._responses.get(key)
            if existing is not None and existing != rows:
                raise ValueError("P5 v3 duplicate entity-resolution query has conflicting evidence")
            self._responses[key] = deepcopy(rows)

    async def search(self, *, query: str, city: str) -> list[dict[str, Any]]:
        return deepcopy(self._responses.get((query, city), []))


__all__ = ["MaterializedResolutionProviderV3", "validate_materialization_v3"]
