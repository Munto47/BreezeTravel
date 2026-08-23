from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import date

import pytest

from app.audit.engine import AuditEngine
from app.audit.models import AuditRunInput, AuditStatus, EvidenceFreshness, EvidenceSnapshot
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    RevisionTransport,
    TripDateRange,
)
from app.repairs.candidates import FrozenRepairCandidateSet, candidate_set_hash
from app.schemas.task_spec import DateRange, TripTaskSpec
from app.trip_check.provider_integrity import ProviderCallReceipt
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v2 import (
    build_evidence_materialization,
    validate_evidence_materialization,
)


def _case(*, freshness: str = "FRESH", fault_profile_id: str = "advice_completeness") -> dict:
    product_input = {
        "source_type": "MANUAL_TEXT",
        "raw_text": "杭州2人，2天。第1天 09:00-10:00 西湖风景名胜区，11:00-12:00 灵隐寺。",
    }
    return {
        "schema_version": "trip-check-p5-eval-case-v2",
        "case_id": f"p5.dev.hz.{freshness.casefold()}",
        "city": "杭州",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "normalized_input_sha256": digest(product_input),
        "product_input": product_input,
        "runner_control": {
            "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v2",
            "fault_profile_id": fault_profile_id,
            "evidence_freshness": freshness,
            "seed": 20260823,
        },
        "oracle": {"must_not_be_read": freshness},
        "expected": {"must_not_be_read": freshness},
    }


def _audit(materialization: dict):
    source = materialization["source_payload"]
    snapshot = EvidenceSnapshot.model_validate(materialization["evidence_snapshot"]["snapshot"])
    trip_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    stops = [
        ItineraryStop(
            stop_id=item["stop_id"],
            place_id=item["place_id"],
            day_index=item["day_index"],
            order_index=item["order_index"],
            start_time=item["start_time"],
            end_time=item["end_time"],
            raw_name=item["display_name"],
            transport_to_next=RevisionTransport(mode="driving"),
        )
        for item in source["stops"]
    ]
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="itin-p5-v2",
            workspace_id=snapshot.workspace_id,
            revision=1,
            source_type=RevisionSource.IMPORT,
            city="杭州",
            date_range=trip_range,
            days=[
                ItineraryDay(day_index=0, date=trip_range.start, stops=stops),
                ItineraryDay(day_index=1, date=trip_range.end, stops=[]),
            ],
            created_by="p5-eval",
            created_at=snapshot.created_at,
        )
    )
    task = TripTaskSpec(
        task_id="task-p5-v2",
        room_id="room-p5-v2",
        task_revision=1,
        city="杭州",
        date_range=DateRange(start=trip_range.start, days=2),
    )
    return AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id=task.task_id,
            task_revision=task.task_revision,
        ),
        revision=revision,
        task_spec=task,
        evidence_snapshot=snapshot,
        now=snapshot.created_at,
    )


@pytest.mark.parametrize(
    ("freshness", "expected_status", "reason_code"),
    [
        ("FRESH", AuditStatus.SATISFIED, "ROUTE_GAP_SUFFICIENT"),
        ("UNAVAILABLE", AuditStatus.UNKNOWN, "ROUTE_GAP_EVIDENCE_UNKNOWN"),
        ("CONFLICTING", AuditStatus.UNKNOWN, "EVIDENCE_CONFLICT"),
    ],
)
def test_materialized_evidence_is_consumed_by_real_audit_engine(
    freshness: str,
    expected_status: AuditStatus,
    reason_code: str,
) -> None:
    materialization = build_evidence_materialization(_case(freshness=freshness))

    snapshot = EvidenceSnapshot.model_validate(materialization["evidence_snapshot"]["snapshot"])
    receipts = [ProviderCallReceipt.model_validate(item) for item in materialization["receipts"]]
    report = _audit(materialization)

    route_facts = [fact for fact in snapshot.facts if fact.fact_type == "ROUTE_TIME"]
    assert {fact.freshness_status for fact in route_facts} == {EvidenceFreshness(freshness)}
    assert receipts
    assert any(finding.status == expected_status and finding.reason_code == reason_code for finding in report.findings)


def test_candidate_sets_are_frozen_and_bind_successful_place_and_route_receipts() -> None:
    materialization = build_evidence_materialization(_case())
    receipts = {
        item.receipt_id: item
        for item in (ProviderCallReceipt.model_validate(raw) for raw in materialization["receipts"])
    }

    assert materialization["candidate_sets"]
    for artifact in materialization["candidate_sets"]:
        candidate_set = FrozenRepairCandidateSet.model_validate(artifact["candidate_set"])
        assert candidate_set.content_hash == candidate_set_hash(
            candidate_set.candidate_set_id,
            candidate_set.candidates,
        )
        for candidate in candidate_set.candidates:
            assert receipts[candidate.place_receipt_id].operation == "place.resolve"
            assert receipts[candidate.place_receipt_id].status == "SUCCEEDED"
            assert candidate.route_receipt_ids
            assert all(receipts[item].operation == "route.candidate" for item in candidate.route_receipt_ids)


def test_empty_candidate_profile_is_absence_not_an_unbound_specific_candidate() -> None:
    materialization = build_evidence_materialization(_case(fault_profile_id="empty_candidate_set"))

    assert materialization["candidate_sets"] == []
    assert validate_evidence_materialization(materialization) == materialization


def test_present_but_empty_candidate_set_fails_closed() -> None:
    materialization = build_evidence_materialization(_case())
    tampered = deepcopy(materialization)
    artifact = tampered["candidate_sets"][0]
    candidate_set_id = artifact["candidate_set"]["candidate_set_id"]
    artifact["candidate_set"] = {
        "candidate_set_id": candidate_set_id,
        "candidates": [],
        "content_hash": candidate_set_hash(candidate_set_id, ()),
    }
    artifact["content_sha256"] = digest({key: value for key, value in artifact.items() if key != "content_sha256"})
    tampered["evidence_materialization_hash"] = digest(
        {key: value for key, value in tampered.items() if key != "evidence_materialization_hash"}
    )

    with pytest.raises(ValueError, match="cannot be empty"):
        validate_evidence_materialization(tampered)


def test_missing_or_tampered_candidate_receipt_fails_closed() -> None:
    materialization = build_evidence_materialization(_case())

    def remove_receipt(payload: dict, receipt_id: str) -> None:
        payload["receipts"] = [item for item in payload["receipts"] if item["receipt_id"] != receipt_id]
        payload["provider_snapshot"]["receipt_ids"] = [
            item for item in payload["provider_snapshot"]["receipt_ids"] if item != receipt_id
        ]
        payload["provider_snapshot"]["content_sha256"] = digest(
            {key: value for key, value in payload["provider_snapshot"].items() if key != "content_sha256"}
        )
        payload["evidence_materialization_hash"] = digest(
            {key: value for key, value in payload.items() if key != "evidence_materialization_hash"}
        )

    missing = deepcopy(materialization)
    receipt_id = missing["candidate_sets"][0]["candidate_set"]["candidates"][0]["place_receipt_id"]
    remove_receipt(missing, receipt_id)
    with pytest.raises(ValueError, match="provider response receipt"):
        validate_evidence_materialization(missing)

    missing_route = deepcopy(materialization)
    route_receipt_id = missing_route["candidate_sets"][0]["candidate_set"]["candidates"][0]["route_receipt_ids"][0]
    remove_receipt(missing_route, route_receipt_id)
    with pytest.raises(ValueError, match="successful route receipt"):
        validate_evidence_materialization(missing_route)

    tampered = deepcopy(materialization)
    tampered["receipts"][0]["response_hash"] = "f" * 64
    tampered["evidence_materialization_hash"] = digest(
        {key: value for key, value in tampered.items() if key != "evidence_materialization_hash"}
    )
    with pytest.raises(ValueError, match="semantic hash mismatch"):
        validate_evidence_materialization(tampered)


def test_oracle_and_expected_never_change_or_leak_into_materialization() -> None:
    left = _case()
    right = deepcopy(left)
    right["oracle"] = {"secret": "changed"}
    right["expected"] = {"secret": "changed-again"}

    assert build_evidence_materialization(left) == build_evidence_materialization(right)
    serialized = str(build_evidence_materialization(left)).casefold()
    assert "oracle" not in serialized
    assert "expected" not in serialized


def test_materializer_never_accesses_or_iterates_label_fields() -> None:
    case = _case()

    class LabelGuard(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            if key in {"oracle", "expected"}:
                raise AssertionError(f"label field was accessed: {key}")
            return case[key]

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("materializer must not iterate the case payload")

        def __len__(self) -> int:
            return len(case)

    artifact = build_evidence_materialization(LabelGuard())

    assert set(artifact) == {
        "schema_version",
        "case_id",
        "source_payload",
        "provider_snapshot",
        "evidence_snapshot",
        "candidate_sets",
        "receipts",
        "evidence_materialization_hash",
    }
    assert "materialization_hash" not in artifact
