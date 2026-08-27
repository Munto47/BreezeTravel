from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.itineraries.models import TripDateRange
from app.itineraries.hash_service import sha256_canonical
from app.trip_check.models import (
    AccommodationBrief,
    ArrivalDeparture,
    BriefFieldConfirmation,
    BriefFieldOrigin,
    BriefFieldProvenance,
    BriefHardness,
    RunBudget,
    RunPartialFailure,
    RunSpec,
    TransportMode,
    TripBriefRevision,
    TripBriefStatus,
    TripCheckRun,
    TripCheckRunStatus,
    TripCheckStage,
)


FIELD_NAMES = {
    "city",
    "date_range",
    "traveler_count",
    "arrival",
    "departure",
    "accommodation",
    "transport_modes",
    "transport_restrictions",
    "budget",
    "dining_style",
    "lodging_style",
    "dietary_restrictions",
    "daily_pace",
    "activity_intensity",
}


def _provenance(*, confirmed: bool = False):
    return {
        name: BriefFieldProvenance(
            confidence=1,
            origin=BriefFieldOrigin.USER_CONFIRMED if confirmed else BriefFieldOrigin.PARSER,
            confirmation=(
                BriefFieldConfirmation.CONFIRMED if confirmed else BriefFieldConfirmation.UNCONFIRMED
            ),
            hardness=BriefHardness.HARD if name in {"city", "date_range", "traveler_count"} else BriefHardness.SOFT,
        )
        for name in FIELD_NAMES
    }


def _brief(**updates):
    payload = {
        "brief_id": "brief-1",
        "workspace_id": "workspace-1",
        "revision": 1,
        "content_hash": "a" * 64,
        "city": "北京",
        "date_range": TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        "traveler_count": 2,
        "arrival": ArrivalDeparture(),
        "departure": ArrivalDeparture(),
        "accommodation": AccommodationBrief(area="东城区"),
        "transport_modes": [TransportMode.WALKING, TransportMode.TRANSIT],
        "field_provenance": _provenance(),
        "status": TripBriefStatus.NEEDS_CONFIRMATION,
        "created_by": "user-1",
    }
    payload.update(updates)
    return TripBriefRevision(**payload)


def test_trip_brief_requires_exact_field_provenance_and_confirmation_receipt():
    draft = _brief()
    assert draft.traveler_count == 2

    with pytest.raises(ValidationError, match="brief field provenance mismatch"):
        _brief(field_provenance={"city": _provenance()["city"]})

    with pytest.raises(ValidationError, match="city, date range, and traveler count"):
        _brief(
            status=TripBriefStatus.CONFIRMED,
            confirmed_by="user-1",
            confirmed_at=datetime.now(timezone.utc),
        )

    required_confirmed = _provenance()
    for field_name in {"city", "date_range", "traveler_count"}:
        required_confirmed[field_name] = required_confirmed[field_name].model_copy(
            update={"confirmation": BriefFieldConfirmation.CONFIRMED}
        )
    confirmed = _brief(
        status=TripBriefStatus.CONFIRMED,
        field_provenance=required_confirmed,
        confirmed_by="user-1",
        confirmed_at=datetime.now(timezone.utc),
    )
    assert confirmed.status == TripBriefStatus.CONFIRMED


def test_inferred_brief_field_cannot_be_hard():
    with pytest.raises(ValidationError, match="INFERRED brief fields cannot be HARD"):
        BriefFieldProvenance(
            confidence=0.5,
            origin=BriefFieldOrigin.INFERRED,
            hardness=BriefHardness.HARD,
        )


def test_trip_brief_accepts_confirmed_domestic_city_outside_legacy_three_city_scope():
    assert _brief(city="成都", traveler_count=12).traveler_count == 12


def _run_spec():
    return RunSpec(
        commit_sha="8cafa26",
        prompt_version="none-p1",
        model_version="none-p1",
        provider_version="controlled-fixture-v1",
        rule_set_version="audit-v1",
        execution_mode="fixture",
        dataset_hash="b" * 64,
        snapshot_hash="c" * 64,
        fault_profile="none",
        random_seed=7,
        budget=RunBudget(timeout_seconds=30),
    )


def test_trip_check_run_preserves_partial_and_lease_invariants():
    spec = _run_spec()
    config_hash = sha256_canonical(spec.model_dump(mode="json"))
    with pytest.raises(ValidationError, match="partial runs require"):
        TripCheckRun(
            run_id="run-1",
            workspace_id="workspace-1",
            itinerary_revision=1,
            brief_id="brief-1",
            brief_revision=1,
            stage=TripCheckStage.COLLECT_EVIDENCE,
            run_spec=spec,
            config_hash=config_hash,
            status=TripCheckRunStatus.PARTIAL,
            created_by="user-1",
        )

    run = TripCheckRun(
        run_id="run-1",
        workspace_id="workspace-1",
        itinerary_revision=1,
        brief_id="brief-1",
        brief_revision=1,
        stage=TripCheckStage.COLLECT_EVIDENCE,
        run_spec=spec,
        config_hash=config_hash,
        status=TripCheckRunStatus.PARTIAL,
        created_by="user-1",
        partial_failures=[
            RunPartialFailure(
                stage=TripCheckStage.COLLECT_EVIDENCE,
                provider="fixture",
                category="ROUTE_FIELD_UNAVAILABLE",
                affected_fields=["route.duration"],
            )
        ],
    )
    assert run.partial_failures[0].category == "ROUTE_FIELD_UNAVAILABLE"
