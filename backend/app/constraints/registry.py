from app.constraints.rules import (
    BudgetRule, CollaborationSnapshotRule, DailyCapacityRule, DailyHotelRule, DuplicateRule, ExclusionRule, HotelAreaRule,
    InclusionRule, MealWindowRule, OpeningHoursRule, TimeChainRule,
    TravelTimeRule, TripAreaRule, WeatherRule,
)


def default_rules():
    return [
        InclusionRule(), ExclusionRule(), DuplicateRule(), DailyCapacityRule(),
        TimeChainRule(), TravelTimeRule(), MealWindowRule(), OpeningHoursRule(),
        BudgetRule(), WeatherRule(), HotelAreaRule(), TripAreaRule(), DailyHotelRule(), CollaborationSnapshotRule(),
    ]
