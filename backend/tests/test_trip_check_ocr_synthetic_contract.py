import json
import re
from collections import Counter
from pathlib import Path


FIXTURE = (
    Path(__file__).parents[1]
    / "evals"
    / "fixtures"
    / "trip_check_ocr_synthetic_v1.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_synthetic_ocr_fixture_has_frozen_provenance_and_privacy_contract():
    payload = _load_fixture()

    assert payload["schema_version"] == "trip-check-ocr-synthetic-v1"
    assert payload["provenance"] == "high_fidelity_synthetic"
    assert payload["evidence_class"] == "synthetic_stress"
    assert payload["generator_model"] == "gpt-5.6-sol"
    assert payload["freeze_policy"]["status"] == "FROZEN_BEFORE_FIRST_OCR"
    assert payload["freeze_policy"]["post_hoc_label_changes_forbidden"] is True
    assert payload["privacy_contract"]["contains_real_personal_information"] is False
    assert payload["privacy_contract"]["contains_human_labels"] is False
    assert payload["privacy_contract"]["generated_images_must_not_be_committed"] is True


def test_synthetic_ocr_fixture_matches_distribution_and_scope_contract():
    payload = _load_fixture()
    cases = payload["cases"]
    contract = payload["distribution_contract"]

    assert len(cases) == contract["case_count"] == 12
    assert Counter(case["city"] for case in cases) == contract["cities"]
    assert Counter(case["image"]["format"] for case in cases) == contract["formats"]
    assert Counter(case["layout"] for case in cases) == contract["layouts"]
    assert Counter(case["difficulty"] for case in cases) == contract["difficulties"]
    assert Counter(case["theme"] for case in cases) == contract["themes"]
    assert all(2 <= case["traveler_count"] <= 5 for case in cases)
    assert all(2 <= case["day_count"] <= 5 for case in cases)
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert len({case["seed"] for case in cases}) == len(cases)
    assert all(case["text_blocks"] for case in cases)
    assert all(case["image"]["width"] > 0 and case["image"]["height"] > 0 for case in cases)
    assert all(
        {
            "rotation_deg",
            "scale",
            "compression_quality",
            "crop",
            "blur_radius",
            "noise_sigma",
            "occlusions",
            "chat_noise",
        }
        == case["render_profile"].keys()
        for case in cases
    )
    assert all(
        len({block["block_id"] for block in case["text_blocks"]})
        == len(case["text_blocks"])
        for case in cases
    )


def test_synthetic_ocr_fixture_key_fields_are_stable_and_confirmations_are_realistic():
    payload = _load_fixture()
    cases = payload["cases"]
    all_fields = [field for case in cases for field in case["oracle"]["key_fields"]]
    confirm_cases = [
        case
        for case in cases
        if any(field["must_confirm"] for field in case["oracle"]["key_fields"])
    ]
    confirm_fields = [field for field in all_fields if field["must_confirm"]]

    assert len({field["field_id"] for field in all_fields}) == len(all_fields)
    assert all({"field_id", "type", "value", "must_confirm"} <= field.keys() for field in all_fields)
    assert len(confirm_cases) >= payload["distribution_contract"]["must_confirm_min_cases"]
    assert len(confirm_fields) >= payload["distribution_contract"]["must_confirm_min_fields"]
    assert all(case["difficulty"] == "hard" for case in confirm_cases)
    assert all(case["render_profile"]["blur_radius"] > 0 for case in confirm_cases)
    assert all(case["render_profile"]["occlusions"] for case in confirm_cases)


def test_synthetic_ocr_fixture_covers_required_input_semantics():
    payload = _load_fixture()
    field_types = {
        field["type"]
        for case in payload["cases"]
        for field in case["oracle"]["key_fields"]
    }
    transport_values = {
        field["value"]
        for case in payload["cases"]
        for field in case["oracle"]["key_fields"]
        if field["type"] == "transport_mode"
    }

    assert {
        "city",
        "traveler_count",
        "day_count",
        "date",
        "arrival",
        "departure",
        "hotel",
        "lodging_area",
        "poi",
        "time",
        "transport_mode",
        "preference",
        "constraint",
    } <= field_types
    assert transport_values == {"walking", "transit", "bicycling", "driving"}


def test_synthetic_ocr_fixture_has_no_contact_or_network_identifiers():
    payload = _load_fixture()
    rendered_text = "\n".join(
        block["text"]
        for case in payload["cases"]
        for block in case["text_blocks"]
    )

    assert re.search(r"https?://|www\.", rendered_text, flags=re.IGNORECASE) is None
    assert re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", rendered_text) is None
    assert re.search(r"(?:账号|账户|邮箱|微信号|身份证)", rendered_text) is None
