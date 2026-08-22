from scripts.validate_trip_check_pilot import validate_pilot


def test_p1_pilot_is_18_cases_with_strict_three_city_distribution():
    result = validate_pilot()
    assert result == {
        "schema_version": "trip-check-pilot-validation-v1",
        "valid": True,
        "execution_status": "NOT_RUN",
        "case_count": 18,
        "city_counts": {"上海": 6, "北京": 6, "杭州": 6},
        "errors": [],
    }
