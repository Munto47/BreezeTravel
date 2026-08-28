from __future__ import annotations

from evals.agent_gate_v1.contracts import SealedAgentBlindThresholds


def evaluate_frozen_thresholds(
    *,
    metrics: dict[str, float | int | bool],
    thresholds: SealedAgentBlindThresholds,
) -> bool:
    def condition_passes(metric: str, operator: str, threshold: bool | int | float) -> bool:
        if metric not in metrics:
            return False
        observed = metrics[metric]
        if operator == "EQ":
            return observed == threshold
        if isinstance(observed, bool) or isinstance(threshold, bool):
            return False
        if operator == "GE":
            return observed >= threshold
        return observed <= threshold

    return all(
        condition_passes(item.metric, item.operator, item.value)
        for item in thresholds.conditions
    )
