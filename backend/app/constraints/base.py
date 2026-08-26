from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from app.schemas.itinerary import Itinerary
from app.schemas.task_spec import TripTaskSpec
from app.schemas.verification import ConstraintCheck


@dataclass(frozen=True)
class RuleContext:
    task_spec: TripTaskSpec
    itinerary: Itinerary
    place_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ConstraintRule(Protocol):
    rule_id: str

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]: ...
