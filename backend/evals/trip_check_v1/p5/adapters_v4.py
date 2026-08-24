"""P5 v4 adapter identities over the frozen v3 product execution path.

P5 v4 changes the formal evaluation envelope, not the product behavior.  The
case and materialization payloads therefore remain on their validated v3
contracts while the adapter identity is versioned independently for v4
RunSpecs and Gate readback.
"""

from __future__ import annotations

from evals.trip_check_v1.p5.adapters_v3 import (
    CoreAdapterV3,
    EvaluationCachingPaddleOcrEngineV3,
    LegacyAdapterV3,
    SolverAdapterV3,
    validate_materialization_v3,
)


class LegacyAdapterV4(LegacyAdapterV3):
    adapter_version = "legacy-a-v4"


class CoreAdapterV4(CoreAdapterV3):
    adapter_version = "core-b-v4"


class SolverAdapterV4(SolverAdapterV3):
    adapter_version = "solver-c-v4"


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
    "SolverAdapterV4",
    "validate_materialization_v3",
]
