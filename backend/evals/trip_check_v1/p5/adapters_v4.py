"""P5 v4 adapter identities over the frozen v3 product execution path.

P5 v4 changes the formal evaluation envelope, not the product behavior.  The
case and materialization payloads therefore remain on their validated v3
contracts while the adapter identity is versioned independently for v4
RunSpecs and Gate readback.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from evals.trip_check_v1.p5.adapters_v2 import (
    LegacyAdapterV2,
    _execute_product_harness,
)
from evals.trip_check_v1.p5.adapters_v3 import (
    CoreAdapterV3,
    EvaluationCachingPaddleOcrEngineV3,
    LegacyAdapterV3,
    SolverAdapterV3,
    _as_v3_result,
)
from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3, P5VariantRunSpecV3
from evals.trip_check_v1.p5.data_contract_v4 import validate_materialization_v4


class MaterializedResolutionProviderV4:
    """Replay already-validated v4 place candidates without a v3 rebuild."""

    def __init__(self, materialization: Mapping[str, Any]) -> None:
        source = materialization.get("source_payload")
        receipts = materialization.get("receipts")
        if not isinstance(source, Mapping) or not isinstance(receipts, list):
            raise ValueError("P5 v4 resolution materialization is incomplete")
        receipt_by_id = {
            str(item.get("receipt_id")): item
            for item in receipts
            if isinstance(item, Mapping) and item.get("operation") == "place.search"
        }
        resolutions = source.get("entity_resolutions")
        if not isinstance(resolutions, list):
            raise ValueError("P5 v4 resolution materialization has no resolutions")
        target_city = str(source.get("city"))
        self._responses: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for resolution in resolutions:
            if not isinstance(resolution, Mapping):
                raise ValueError("P5 v4 resolution entry is invalid")
            receipt = receipt_by_id.get(str(resolution.get("search_receipt_id")))
            candidates = resolution.get("candidates")
            if not isinstance(receipt, Mapping) or not isinstance(candidates, list):
                raise ValueError("P5 v4 resolution receipt binding is incomplete")
            rows = [
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
                for candidate in candidates
                if isinstance(candidate, Mapping)
            ]
            if len(rows) != len(candidates):
                raise ValueError("P5 v4 resolution candidate is invalid")
            key = (str(resolution.get("raw_name")), target_city)
            if key in self._responses and self._responses[key] != rows:
                raise ValueError("P5 v4 duplicate resolution evidence conflicts")
            self._responses[key] = rows

    async def search(self, *, query: str, city: str) -> list[dict[str, Any]]:
        return deepcopy(self._responses.get((query, city), []))


class LegacyAdapterV4(LegacyAdapterV3):
    adapter_version = "legacy-a-v4"

    def __init__(self) -> None:
        self._delegate = LegacyAdapterV2(
            materialization_validator=validate_materialization_v4,
            contract_version="v3",
        )


class CoreAdapterV4(CoreAdapterV3):
    adapter_version = "core-b-v4"

    async def execute(
        self,
        case: P5CaseV3,
        materialization: Mapping[str, Any],
        run_spec: P5VariantRunSpecV3,
    ) -> Any:
        validated = validate_materialization_v4(case, materialization)
        if case.input_kind == "SYNTHETIC_SCREENSHOT" and self._ocr_engine is None:
            raise ValueError("P5 v4 screenshots require the fail-closed frozen OCR cache")
        runtime_case = case.model_copy(update={"oracle": None})
        result = await _execute_product_harness(
            runtime_case,
            validated,
            run_spec,
            strategy=self.repair_strategy,
            ocr_engine=self._ocr_engine,
            candidate_provider_factory=MaterializedResolutionProviderV4,
            contract_version="v3",
        )
        return _as_v3_result(result)


class SolverAdapterV4(SolverAdapterV3):
    adapter_version = "solver-c-v4"

    async def execute(
        self,
        case: P5CaseV3,
        materialization: Mapping[str, Any],
        run_spec: P5VariantRunSpecV3,
    ) -> Any:
        validated = validate_materialization_v4(case, materialization)
        if case.input_kind == "SYNTHETIC_SCREENSHOT" and self._ocr_engine is None:
            raise ValueError("P5 v4 screenshots require the fail-closed frozen OCR cache")
        runtime_case = case.model_copy(update={"oracle": None})
        result = await _execute_product_harness(
            runtime_case,
            validated,
            run_spec,
            strategy=self.repair_strategy,
            ocr_engine=self._ocr_engine,
            candidate_provider_factory=MaterializedResolutionProviderV4,
            contract_version="v3",
        )
        return _as_v3_result(result)


ADAPTERS_V4 = {
    "legacy_a": LegacyAdapterV4,
    "core_b": CoreAdapterV4,
    "solver_c": SolverAdapterV4,
}

ADAPTER_VERSIONS_V4 = {
    variant_id: (adapter.adapter_version, adapter.repair_strategy)
    for variant_id, adapter in ADAPTERS_V4.items()
}


__all__ = [
    "ADAPTERS_V4",
    "ADAPTER_VERSIONS_V4",
    "CoreAdapterV4",
    "EvaluationCachingPaddleOcrEngineV3",
    "LegacyAdapterV4",
    "MaterializedResolutionProviderV4",
    "SolverAdapterV4",
    "validate_materialization_v4",
]
