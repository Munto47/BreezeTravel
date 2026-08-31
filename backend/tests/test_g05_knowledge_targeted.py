from datetime import datetime, timedelta, timezone

from app.trip_understanding.knowledge import (
    KnowledgeClaimCandidate,
    project_knowledge_suggestions,
    select_knowledge_candidates,
)
from app.trip_understanding.models import (
    ActivityCardView,
    AssumptionChipView,
    MapReadinessView,
    StaySuggestionView,
    TripDayView,
    UserFacingTripResult,
)


NOW = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)


def _candidate(
    *,
    revision_id: str = "claim-revision-1",
    claim_key: str = "claim-key-1",
    claim_type: str = "RESERVATION_ADVICE",
    conditions_hash: str = "a" * 64,
    text: str = "建议提前预约。",
    source_status: str = "ADMITTED",
    license_status: str = "FACTS_ONLY_WITH_ATTRIBUTION",
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
    source_expires_at: datetime | None = None,
    claim_withdrawn_at: datetime | None = None,
    source_withdrawn_at: datetime | None = None,
) -> KnowledgeClaimCandidate:
    return KnowledgeClaimCandidate(
        claim_revision_id=revision_id,
        claim_key=claim_key,
        claim_version=1,
        canonical_place_id="B000A7BD6T",
        claim_type=claim_type,
        conditions_hash=conditions_hash,
        suggestion_text=text,
        short_evidence="官方页面说明需要预约。",
        effective_at=effective_at or NOW - timedelta(days=1),
        expires_at=expires_at or NOW + timedelta(days=30),
        claim_withdrawn_at=claim_withdrawn_at,
        source_version_id="source-version-1",
        source_name="官方来源",
        source_url="https://example.gov.cn/place",
        source_observed_at=NOW - timedelta(days=1),
        source_expires_at=source_expires_at or NOW + timedelta(days=30),
        source_admission_status=source_status,
        source_license_status=license_status,
        source_withdrawn_at=source_withdrawn_at,
    )


def _result() -> UserFacingTripResult:
    return UserFacingTripResult(
        status="READY",
        assumptions=[
            AssumptionChipView(
                key="destination",
                label="目的地",
                value="北京",
                editable=True,
            )
        ],
        days=[
            TripDayView(
                label="Day 1",
                activities=[
                    ActivityCardView(
                        activity_token="activity-token-00000000001",
                        name="故宫博物院",
                        category="景点",
                        area_or_address="北京市东城区",
                        status="READY",
                        available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
                    ),
                    ActivityCardView(
                        activity_token="activity-token-00000000002",
                        name="地点待确认",
                        category="地点",
                        area_or_address="地点待确认",
                        status="NEEDS_CONFIRMATION",
                        available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
                    ),
                ],
            )
        ],
        map=MapReadinessView(status="PREPARING", message="地图准备中"),
        stay=StaySuggestionView(status="PREPARING", message="住宿建议准备中"),
        available_actions=["EDIT_ASSUMPTIONS", "EDIT_CARDS"],
    )


def test_g05_filters_unauthorized_expired_and_withdrawn_claims() -> None:
    candidates = [
        _candidate(revision_id="unauthorized", source_status="NOT_READY"),
        _candidate(
            revision_id="expired",
            claim_key="expired",
            expires_at=NOW,
        ),
        _candidate(
            revision_id="claim-withdrawn",
            claim_key="claim-withdrawn",
            claim_withdrawn_at=NOW - timedelta(minutes=1),
        ),
        _candidate(
            revision_id="source-withdrawn",
            claim_key="source-withdrawn",
            source_withdrawn_at=NOW - timedelta(minutes=1),
        ),
    ]

    selected, conflicts = select_knowledge_candidates(candidates, now=NOW)

    assert selected == {}
    assert conflicts == ()


def test_g05_does_not_apply_future_source_or_withdrawal_early() -> None:
    future_source = _candidate(revision_id="future-source").model_copy(
        update={"source_observed_at": NOW + timedelta(minutes=5)}
    )
    scheduled_withdrawal = _candidate(
        revision_id="scheduled-withdrawal",
        claim_key="scheduled-withdrawal",
        claim_withdrawn_at=NOW + timedelta(minutes=5),
    )

    selected, _ = select_knowledge_candidates(
        [future_source, scheduled_withdrawal],
        now=NOW,
    )

    assert [item.claim_revision_id for item in selected["B000A7BD6T"]] == [
        "scheduled-withdrawal"
    ]


def test_g05_conflicting_claims_are_all_suppressed() -> None:
    candidates = [
        _candidate(revision_id="conflict-a", claim_key="conflict-a", text="建议上午前往。"),
        _candidate(revision_id="conflict-b", claim_key="conflict-b", text="建议晚上前往。"),
    ]

    selected, conflicts = select_knowledge_candidates(candidates, now=NOW)

    assert selected == {}
    assert conflicts == (("B000A7BD6T", "RESERVATION_ADVICE", "a" * 64),)


def test_g05_projection_is_dynamic_bounded_and_does_not_change_authoritative_fields() -> None:
    original = _result()
    candidates = [
        _candidate(),
        _candidate(
            revision_id="duration",
            claim_key="duration",
            claim_type="TYPICAL_DURATION",
            conditions_hash="b" * 64,
            text="可预留约半天游览。",
        ),
        _candidate(
            revision_id="time",
            claim_key="time",
            claim_type="SUITABLE_TIME",
            conditions_hash="c" * 64,
            text="上午抵达通常更从容。",
        ),
        _candidate(
            revision_id="season",
            claim_key="season",
            claim_type="SEASON",
            conditions_hash="d" * 64,
            text="出发前留意季节开放调整。",
        ),
    ]

    projected = project_knowledge_suggestions(
        original,
        canonical_place_by_activity_token={
            "activity-token-00000000001": "B000A7BD6T",
            "activity-token-00000000002": "B000A7BD6T",
        },
        candidates=candidates,
        now=NOW,
    )

    card = projected.result.days[0].activities[0]
    assert len(card.knowledge_suggestions) == 3
    assert [item.type for item in card.knowledge_suggestions] == [
        "RESERVATION_ADVICE",
        "TYPICAL_DURATION",
        "SUITABLE_TIME",
    ]
    assert projected.result.days[0].activities[1].knowledge_suggestions == []
    assert original.days[0].activities[0].knowledge_suggestions == []
    without_suggestions = projected.result.model_copy(deep=True)
    for day in without_suggestions.days:
        for activity in day.activities:
            activity.knowledge_suggestions = []
    assert without_suggestions == original


def test_g05_public_projection_contains_no_internal_identifiers_or_receipts() -> None:
    projection = project_knowledge_suggestions(
        _result(),
        canonical_place_by_activity_token={"activity-token-00000000001": "B000A7BD6T"},
        candidates=[_candidate()],
        now=NOW,
    )
    public_json = projection.result.model_dump_json()

    assert "claim-revision-1" not in public_json
    assert "source-version-1" not in public_json
    assert "license_status" not in public_json
    assert "receipt" not in public_json.lower()
    assert "confidence" not in public_json.lower()
