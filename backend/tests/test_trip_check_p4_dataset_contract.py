from scripts.validate_trip_check_p4_datasets import validate


def test_p4_dataset_contract_is_balanced_isolated_private_and_hash_bound():
    result = validate()

    assert result["status"] == "PASS", result["errors"]
    assert result["distribution"]["dev"]["city_counts"] == {
        "上海": 60, "北京": 60, "杭州": 60
    }
    assert result["distribution"]["regression"]["city_counts"] == {
        "上海": 24, "北京": 24, "杭州": 24
    }
    assert result["pilot_count"] == 18
    assert result["frozen_blind_count"] == 0
    assert all(
        len(result["distribution"][split]["fault_counts"]) == 9
        for split in ("dev", "regression")
    )
