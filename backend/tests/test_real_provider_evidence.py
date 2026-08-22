"""Offline contract tests for the opt-in real-provider evidence collector."""

from scripts.verify_real_providers import (
    CITY_CASES,
    EVIDENCE_CLASS,
    FIXED_CITIES,
    _canonical_hash,
    classify_provider_error,
    percentile,
    validate_report,
)


def test_fixed_samples_cover_beijing_shanghai_hangzhou():
    assert tuple(case["city"] for case in CITY_CASES) == FIXED_CITIES
    assert all(case["entity"] for case in CITY_CASES)
    assert all(case["origin"]["label"] and case["destination"]["label"] for case in CITY_CASES)


def test_canonical_hash_is_stable_and_does_not_embed_input():
    value = {"path": "/v5/place/text", "region": "北京"}
    assert _canonical_hash(value) == _canonical_hash({"region": "北京", "path": "/v5/place/text"})
    assert "北京" not in _canonical_hash(value)


def test_percentile_uses_linear_interpolation():
    assert percentile([10, 20, 30], 0.50) == 20
    assert percentile([10, 20, 30], 0.95) == 29
    assert percentile([], 0.95) is None


def test_provider_error_classification_keeps_auth_rate_and_server_distinct():
    assert classify_provider_error("amap", "10001", 200) == "authentication_or_authorization"
    assert classify_provider_error("qweather", "429", 200) == "rate_limited"
    assert classify_provider_error("qweather", "500", 503) == "provider_http_5xx"
    assert classify_provider_error("amap", "10003", 200) == "provider_business_error"


def test_validation_rejects_mock_and_failed_provider_sample():
    report = {
        "evidence_class": EVIDENCE_CLASS,
        "runtime": {"amap_mock": True, "demo_mode": False},
        "fixed_cities": list(FIXED_CITIES),
        "iterations": 1,
        "samples": [
            {
                "case_id": f"case-{index}",
                "request_hash": "sha256:x",
                "response_hash": "sha256:y",
                "observed_at": "2026-08-20T00:00:00Z",
                "status": "error" if index == 0 else "ok",
                "latency_ms": 1,
                "result_count": 0 if index == 0 else 1,
                "error_category": "timeout" if index == 0 else None,
            }
            for index in range(9)
        ],
    }
    errors = validate_report(report)
    assert "amap_mock_must_be_false" in errors
    assert any(error.startswith("provider_failure:case-0:timeout") for error in errors)


def test_validation_accepts_complete_live_contract():
    report = {
        "evidence_class": EVIDENCE_CLASS,
        "runtime": {"amap_mock": False, "demo_mode": False},
        "fixed_cities": list(FIXED_CITIES),
        "iterations": 1,
        "samples": [
            {
                "case_id": f"case-{index}",
                "request_hash": "sha256:x",
                "response_hash": "sha256:y",
                "observed_at": "2026-08-20T00:00:00Z",
                "status": "ok",
                "latency_ms": 1,
                "result_count": 1,
                "error_category": None,
            }
            for index in range(9)
        ],
    }
    assert validate_report(report) == []
