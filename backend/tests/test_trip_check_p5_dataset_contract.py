from scripts.validate_trip_check_p5_dataset import validate


def test_p5_dataset_is_360_cases_with_strict_three_city_and_blind_isolation():
    result = validate()

    assert result["status"] == "PASS", result["errors"]
    assert result["counts"] == {
        "total": 360,
        "by_split": {"dev": 180, "frozen_blind": 90, "pilot": 18, "regression": 72},
        "by_city": {"上海": 120, "北京": 120, "杭州": 120},
    }
    assert result["blind"]["label_payload_in_repository"] is False
    assert result["legacy_overlap_debt"]["regression_fixture_hashes_overlapping_dev"] == 72
    assert result["legacy_overlap_debt"]["regression_oracle_hashes_overlapping_dev"] == 72
