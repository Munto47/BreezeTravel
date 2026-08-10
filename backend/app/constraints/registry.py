from app.constraints.rules import (
    BudgetRule, CollaborationSnapshotRule, DailyCapacityRule, DuplicateRule, ExclusionRule, HotelAreaRule,
    InclusionRule, MealWindowRule, OpeningHoursRule, TimeChainRule,
    TravelTimeRule, WeatherRule,
)


def default_rules():
    return [
        InclusionRule(), ExclusionRule(), DuplicateRule(), DailyCapacityRule(),
        TimeChainRule(), TravelTimeRule(), MealWindowRule(), OpeningHoursRule(),
        BudgetRule(), WeatherRule(), HotelAreaRule(), CollaborationSnapshotRule(),
    ]
