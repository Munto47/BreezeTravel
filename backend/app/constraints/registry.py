from dataclasses import dataclass

from app.constraints.rules import (
    BudgetRule, CollaborationSnapshotRule, DailyCapacityRule, DailyHotelRule, DuplicateRule, ExclusionRule, HotelAreaRule,
    InclusionRule, MealWindowRule, OpeningHoursRule, PacingRule, TimeChainRule,
    TravelTimeRule, TripAreaRule, WeatherRule,
)


@dataclass(frozen=True)
class RuleDescriptor:
    rule: object
    version: str
    dependencies: tuple[str, ...]


def default_rule_descriptors() -> list[RuleDescriptor]:
    """Single authoritative registry for legacy and new AuditEngine adapters."""

    return [
        RuleDescriptor(InclusionRule(), "1.0.0", ("DAY_ORDER",)),
        RuleDescriptor(ExclusionRule(), "1.0.0", ("DAY_ORDER",)),
        RuleDescriptor(DuplicateRule(), "1.0.0", ("DAY_ORDER",)),
        RuleDescriptor(DailyCapacityRule(), "1.0.0", ("DAY_ORDER", "MEMBER_CONSTRAINT")),
        RuleDescriptor(TimeChainRule(), "1.0.0", ("DAY_ORDER", "TIME_WINDOW")),
        RuleDescriptor(TravelTimeRule(), "1.0.0", ("DAY_ORDER", "ROUTE_EDGE")),
        RuleDescriptor(MealWindowRule(), "1.0.0", ("DAY_ORDER", "TIME_WINDOW")),
        RuleDescriptor(PacingRule(), "1.0.0", ("DAY_ORDER", "TIME_WINDOW")),
        RuleDescriptor(OpeningHoursRule(), "1.0.0", ("TIME_WINDOW", "EVIDENCE_FRESHNESS")),
        RuleDescriptor(BudgetRule(), "1.0.0", ("GLOBAL_BUDGET", "EVIDENCE_FRESHNESS")),
        RuleDescriptor(WeatherRule(), "1.0.0", ("WEATHER", "EVIDENCE_FRESHNESS")),
        RuleDescriptor(HotelAreaRule(), "1.0.0", ("HOTEL", "ROUTE_EDGE")),
        RuleDescriptor(TripAreaRule(), "1.0.0", ("DAY_ORDER", "EVIDENCE_FRESHNESS")),
        RuleDescriptor(DailyHotelRule(), "1.0.0", ("HOTEL", "DAY_ORDER")),
        RuleDescriptor(CollaborationSnapshotRule(), "1.0.0", ("MEMBER_CONSTRAINT",)),
    ]


def default_rules():
    return [descriptor.rule for descriptor in default_rule_descriptors()]
