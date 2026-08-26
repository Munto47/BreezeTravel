from __future__ import annotations

import pytest

from app.importing.entity_resolver import EntityResolver
from app.importing.models import RawStop, SourceSpan
from app.itineraries.models import ResolutionStatus


class EchoProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, *, query: str, city: str) -> list[dict]:
        self.calls += 1
        return [{
            "place_id": "controlled-exact",
            "name": query,
            "city": city,
            "address": "受控测试地址",
            "category": "attraction",
            "coords": {"lng": 116.397, "lat": 39.918},
            "retrieval_provider": "controlled_test",
            "retrieval_request_hash": "6" * 64,
            "execution_mode": "fixture",
            "retrieval_response_hash": "f" * 64,
            "retrieval_observed_at": "2026-08-21T00:00:00+00:00",
        }]


def _raw_stop(name: str) -> RawStop:
    return RawStop(
        raw_stop_id="raw-1",
        import_id="import-1",
        day_index=0,
        raw_name=name,
        source_span=SourceSpan(start=0, end=len(name)),
        source_sentence=name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["待定", " 待确认。", "未知地点", "某景点", "随便逛逛"])
async def test_generic_placeholder_fails_closed_before_provider(name: str) -> None:
    provider = EchoProvider()

    result = await EntityResolver(provider).resolve(_raw_stop(name), city="北京")

    assert result.resolution_status == ResolutionStatus.NOT_FOUND
    assert result.canonical_place_id is None
    assert result.confidence == 0
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_real_place_still_uses_provider() -> None:
    provider = EchoProvider()

    result = await EntityResolver(provider).resolve(_raw_stop("故宫博物院"), city="北京")

    assert result.resolution_status == ResolutionStatus.AUTO_MATCHED
    assert result.canonical_place_id == "controlled-exact"
    assert provider.calls == 1
