from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.importing.models import (
    ImportSourceType,
    ImportStatus,
    ItineraryImport,
    RejectedPlaceCandidate,
    ResolutionRejectionReason,
    ResolvedPlaceReceipt,
    ResolvedStop,
    RawStop,
    SourceSpan,
)
from app.importing.repositories import (
    InMemoryImportRepository,
    _resolution_candidates_payload,
    _resolution_from_row,
)
from app.itineraries.errors import InvalidEditCommandError
from app.itineraries.models import ResolutionStatus
from app.schemas.place import RetrievalExecutionMode


def _receipt() -> ResolvedPlaceReceipt:
    return ResolvedPlaceReceipt(
        canonical_place_id="shanghai-tower",
        provider="controlled_test",
        provider_place_id="amap-shanghai-tower",
        name="东方明珠",
        city="上海市",
        district="浦东新区",
        address="世纪大道1号",
        category="attraction",
        longitude=121.4997,
        latitude=31.2397,
        request_hash="3" * 64,
        response_hash="c" * 64,
        observed_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        execution_mode=RetrievalExecutionMode.FIXTURE,
        source_url="https://example.test/provider/shanghai-tower",
    )


def _rejected_resolution() -> ResolvedStop:
    return ResolvedStop(
        raw_stop_id="raw-wrong-city",
        resolution_status=ResolutionStatus.NOT_FOUND,
        rejected_candidates=[
            RejectedPlaceCandidate(
                place_id="shanghai-tower",
                name="东方明珠",
                reason=ResolutionRejectionReason.WRONG_CITY,
                target_city="北京",
                resolved_place_receipt=_receipt(),
            )
        ],
    )


def _row(candidates_json):
    return {
        "raw_stop_id": "raw-wrong-city",
        "canonical_place_id": None,
        "candidates_json": candidates_json,
        "confidence": 0.0,
        "resolution_status": "NOT_FOUND",
        "resolution_version": 1,
        "confirmed_by": None,
        "confirmed_at": None,
    }


def test_resolution_jsonb_roundtrip_preserves_full_rejected_provider_receipt():
    source = _rejected_resolution()

    restored = _resolution_from_row(_row(_resolution_candidates_payload(source)))

    assert restored.candidates == []
    assert restored.rejected_candidates == source.rejected_candidates
    rejected = restored.rejected_candidates[0]
    assert rejected.resolved_place_receipt.request_hash == "3" * 64
    assert rejected.resolved_place_receipt.response_hash == "c" * 64
    assert rejected.resolved_place_receipt.provider_place_id == "amap-shanghai-tower"
    assert rejected.resolved_place_receipt.source_url == ("https://example.test/provider/shanghai-tower")


def test_legacy_candidate_array_readback_does_not_invent_rejected_receipt():
    restored = _resolution_from_row(_row([]))

    assert restored.candidates == []
    assert restored.rejected_candidates == []


def test_model_rejects_canonical_promotion_of_hard_rejected_candidate():
    with pytest.raises(
        ValidationError,
        match="rejected candidates cannot become the canonical resolution",
    ):
        ResolvedStop(
            raw_stop_id="raw-wrong-city",
            canonical_place_id="shanghai-tower",
            resolution_status=ResolutionStatus.AUTO_MATCHED,
            rejected_candidates=_rejected_resolution().rejected_candidates,
        )


@pytest.mark.asyncio
async def test_persisted_rejected_candidate_remains_non_confirmable():
    repository = InMemoryImportRepository()
    resolution = _resolution_from_row(_row(_resolution_candidates_payload(_rejected_resolution())))
    raw_stop = RawStop(
        raw_stop_id="raw-wrong-city",
        import_id="import-wrong-city",
        day_index=0,
        raw_name="东方明珠",
        source_span=SourceSpan(start=0, end=4),
        source_sentence="东方明珠",
    )
    await repository.create_import(
        ItineraryImport(
            import_id="import-wrong-city",
            workspace_id="workspace-beijing",
            source_type=ImportSourceType.MANUAL_TEXT,
            raw_text="东方明珠",
            parse_version="test",
            status=ImportStatus.NEEDS_RESOLUTION,
            raw_stops=[raw_stop],
            resolutions=[resolution],
            created_by="user-test",
        )
    )

    with pytest.raises(
        InvalidEditCommandError,
        match="not an offered resolution candidate",
    ):
        await repository.confirm_resolution(
            "import-wrong-city",
            "raw-wrong-city",
            "shanghai-tower",
            "user-test",
        )
