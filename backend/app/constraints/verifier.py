from __future__ import annotations

from typing import Any, Iterable, Optional

from app.constraints.base import ConstraintRule, RuleContext
from app.constraints.registry import default_rules
from app.schemas.itinerary import Itinerary
from app.schemas.task_spec import TripTaskSpec
from app.schemas.verification import VerificationReport
from app.services.planning_hash import compute_planning_input_hash
from app.observability.metrics import metrics


class ItineraryVerifier:
    def __init__(self, rules: Optional[Iterable[ConstraintRule]] = None):
        self.rules = list(rules or default_rules())

    def verify(
        self,
        task_spec: TripTaskSpec,
        itinerary: Itinerary,
        *,
        places: Iterable[Any],
        planning_input_hash: Optional[str] = None,
        place_meta: Optional[dict[str, dict[str, Any]]] = None,
        repair_rounds: int = 0,
        unresolved_reasons: Optional[list[str]] = None,
    ) -> VerificationReport:
        expected_hash = planning_input_hash or compute_planning_input_hash(task_spec, places, itinerary.version)
        context = RuleContext(task_spec=task_spec, itinerary=itinerary, place_meta=place_meta or {})
        checks = [check for rule in self.rules for check in rule.evaluate(context)]
        for check in checks:
            metrics.inc("constraint_check_total", constraint_type=check.constraint_id.split(":", 1)[0], status=check.status.value)
            if check.status.value == "UNKNOWN":
                metrics.inc("constraint_unknown_total", constraint_type=check.constraint_id.split(":", 1)[0])
        return VerificationReport.from_checks(
            task_id=task_spec.task_id,
            task_revision=task_spec.task_revision,
            itinerary_id=itinerary.itinerary_id,
            itinerary_version=itinerary.version,
            planning_input_hash=expected_hash,
            checks=checks,
            repair_rounds=repair_rounds,
            unresolved_reasons=unresolved_reasons,
        )
