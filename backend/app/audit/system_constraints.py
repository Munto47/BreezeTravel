from __future__ import annotations

from app.schemas.task_spec import HardConstraint, TripTaskSpec


def with_system_constraints(task_spec: TripTaskSpec) -> TripTaskSpec:
    """Attach canonical product invariants without mutating the TaskSpec."""

    existing_types = {item.type for item in task_spec.hard_constraints}
    additions = []
    if "daily_hotel" not in existing_types:
        additions.append(HardConstraint(
            id="system:daily_hotel",
            type="daily_hotel",
            value=True,
            scope="overnight_days",
        ))
    if "system_pacing" not in existing_types:
        additions.append(HardConstraint(
            id="system:pacing",
            type="system_pacing",
            value=True,
            scope="per_day",
        ))
    if "avoid_outdoor_on_rain" not in existing_types:
        additions.append(HardConstraint(
            id="system:weather_exposure",
            type="avoid_outdoor_on_rain",
            value=True,
            scope="per_day",
        ))
    if not additions:
        return task_spec
    return task_spec.model_copy(update={
        "hard_constraints": [*task_spec.hard_constraints, *additions],
    })
