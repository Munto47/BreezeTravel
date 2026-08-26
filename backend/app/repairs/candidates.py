"""Fail-closed CandidateSet binding for repair advice.

The HTTP models remain unchanged.  This module is an internal boundary that
prevents a place-changing RepairOption from becoming user-facing Advice unless
the exact canonical place and its place/route receipts were frozen together.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.itineraries.hash_service import sha256_canonical
from app.repairs.errors import UnverifiedCandidateRejectedError
from app.repairs.models import RepairOperationType, RepairOption


class FrozenRepairCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_place_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    place_receipt_id: str = Field(min_length=1)
    route_receipt_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def receipt_ids_are_unique(self) -> "FrozenRepairCandidate":
        if len(self.route_receipt_ids) != len(set(self.route_receipt_ids)):
            raise ValueError("route receipt ids must be unique")
        if self.place_receipt_id in self.route_receipt_ids:
            raise ValueError("place and route receipts must be distinct")
        return self


class FrozenRepairCandidateSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_set_id: str = Field(min_length=1)
    candidates: tuple[FrozenRepairCandidate, ...]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def content_is_unique_and_hash_bound(self) -> "FrozenRepairCandidateSet":
        place_ids = [item.canonical_place_id for item in self.candidates]
        if len(place_ids) != len(set(place_ids)):
            raise ValueError("canonical place IDs must be unique in a CandidateSet")
        expected = candidate_set_hash(self.candidate_set_id, self.candidates)
        if self.content_hash != expected:
            raise ValueError("CandidateSet content hash mismatch")
        return self


class CandidateAdviceBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_set_id: str
    canonical_place_id: str
    receipt_ids: tuple[str, ...] = Field(min_length=2)


def candidate_set_hash(
    candidate_set_id: str,
    candidates: Sequence[FrozenRepairCandidate],
) -> str:
    normalized = sorted(
        (item.model_dump(mode="json") for item in candidates),
        key=lambda item: (item["canonical_place_id"], item["place_receipt_id"]),
    )
    return sha256_canonical({"candidate_set_id": candidate_set_id, "candidates": normalized})


def freeze_candidate_set(
    candidate_set_id: str,
    candidates: Sequence[FrozenRepairCandidate],
) -> FrozenRepairCandidateSet:
    ordered = tuple(sorted(candidates, key=lambda item: item.canonical_place_id))
    return FrozenRepairCandidateSet(
        candidate_set_id=candidate_set_id,
        candidates=ordered,
        content_hash=candidate_set_hash(candidate_set_id, ordered),
    )


def candidate_binding_for_option(
    option: RepairOption,
    candidate_sets: Mapping[str, FrozenRepairCandidateSet],
) -> CandidateAdviceBinding | None:
    place_operations = [
        operation
        for operation in option.operations
        if operation.operation in {
            RepairOperationType.REPLACE_STOP,
            RepairOperationType.CHANGE_HOTEL_AREA,
        }
    ]
    if not place_operations:
        return None
    if len(place_operations) != 1:
        raise UnverifiedCandidateRejectedError(
            "one repair option cannot select multiple specific replacement places",
            context={"repair_id": option.repair_id},
        )
    payload = place_operations[0].payload
    candidate_set_id = payload.get("candidate_set_id")
    candidate_place_id = payload.get("candidate_place_id")
    if not isinstance(candidate_set_id, str) or not isinstance(candidate_place_id, str):
        raise UnverifiedCandidateRejectedError(
            "specific place repair lacks a frozen CandidateSet binding",
            context={"repair_id": option.repair_id},
        )
    candidate_set = candidate_sets.get(candidate_set_id)
    candidate = next(
        (
            item
            for item in candidate_set.candidates
            if item.canonical_place_id == candidate_place_id
        ),
        None,
    ) if candidate_set is not None else None
    if candidate is None:
        raise UnverifiedCandidateRejectedError(
            "specific place is absent from the frozen CandidateSet",
            context={
                "repair_id": option.repair_id,
                "candidate_set_id": candidate_set_id,
                "canonical_place_id": candidate_place_id,
            },
        )
    return CandidateAdviceBinding(
        candidate_set_id=candidate_set_id,
        canonical_place_id=candidate_place_id,
        receipt_ids=(candidate.place_receipt_id, *candidate.route_receipt_ids),
    )
