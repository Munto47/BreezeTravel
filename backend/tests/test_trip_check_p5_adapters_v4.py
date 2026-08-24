from __future__ import annotations

from evals.trip_check_v1.p5.adapters_v3 import (
    CoreAdapterV3,
    LegacyAdapterV3,
    SolverAdapterV3,
)
from evals.trip_check_v1.p5.adapters_v4 import (
    ADAPTERS_V4,
    ADAPTER_VERSIONS_V4,
    CoreAdapterV4,
    LegacyAdapterV4,
    SolverAdapterV4,
)


def test_v4_adapters_only_version_the_formal_adapter_identity() -> None:
    assert issubclass(LegacyAdapterV4, LegacyAdapterV3)
    assert issubclass(CoreAdapterV4, CoreAdapterV3)
    assert issubclass(SolverAdapterV4, SolverAdapterV3)
    assert ADAPTER_VERSIONS_V4 == {
        "legacy_a": ("legacy-a-v4", "legacy_native_only"),
        "core_b": ("core-b-v4", "bounded_repair_v1"),
        "solver_c": ("solver-c-v4", "cp_sat_v1"),
    }


def test_v4_adapter_registry_is_exact_and_does_not_promote_solver() -> None:
    assert ADAPTERS_V4 == {
        "legacy_a": LegacyAdapterV4,
        "core_b": CoreAdapterV4,
        "solver_c": SolverAdapterV4,
    }
    assert ADAPTERS_V4["core_b"].repair_strategy == "bounded_repair_v1"
    assert ADAPTERS_V4["solver_c"].repair_strategy == "cp_sat_v1"
