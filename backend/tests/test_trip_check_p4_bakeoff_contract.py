from scripts.build_trip_check_p4_bakeoff import build
from scripts.validate_trip_check_p4_bakeoff import validate


def test_p4_bakeoff_is_frozen_balanced_and_hash_bound():
    result = validate()

    assert result["status"] == "PASS", result["errors"]
    assert result["case_count"] == 36
    assert result["city_counts"] == {"上海": 12, "北京": 12, "杭州": 12}
    assert result["frozen_blind_count"] == 0


def test_p4_bakeoff_generator_reproduces_checked_in_dataset():
    cases, manifest = build()
    result = validate()

    assert manifest["dataset_hash"] == result["dataset_hash"]
    assert len(cases) == 36
