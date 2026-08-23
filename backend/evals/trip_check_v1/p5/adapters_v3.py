"""P5 v3 eval adapters over receipt-bound entity-resolution materializations."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from evals.trip_check_v1.p5.evidence_materialization_v3 import (
    EVIDENCE_MATERIALIZATION_SCHEMA_V3,
    validate_evidence_materialization_v3,
)


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


__all__ = ["MaterializedResolutionProviderV3"]
