"""Synthetic label annotations preserve identity and reject unsafe rewrites."""
import pytest

from app.trip_understanding.place_labels import normalized_place_label


@pytest.mark.parametrize("source,expected", [
    ("江城博物馆（人民广场馆，免费）", "江城博物馆（人民广场馆）"),
    ("云汀酒店（东街店，需预约）", "云汀酒店（东街店）"),
    ("江城博物馆（之江馆区，免费，需预约）", "江城博物馆（之江馆区）"),
    ("星河美术馆(南馆,票价20元)", "星河美术馆(南馆)"),
    ("江城博物馆（免费预约）", "江城博物馆"),
    ("星河公园（免费，需预约）", "星河公园"),
    ("江城博物馆(门票30元)", "江城博物馆"),
    ("青溪寺（外观或购票入内）", "青溪寺"),
    ("云汀书院（星城中心 62 楼，咖啡看城市全景）", "云汀书院（星城中心62楼）"),
    ("江城博物馆（人民广场馆，周边景色好）", "江城博物馆（人民广场馆，周边景色好）"),
    ("江城博物馆（枫林博物馆，免费）", "江城博物馆（枫林博物馆，免费）"),
    ("江城博物馆（中山路8号，免费）", "江城博物馆（中山路8号，免费）"),
    ("江城博物馆（东馆或西馆，免费）", "江城博物馆（东馆或西馆，免费）"),
    ("江城博物馆（东馆，改到明天，免费）", "江城博物馆（东馆，改到明天，免费）"),
    ("江城博物馆（东馆，https://example.test，免费）", "江城博物馆（东馆，https://example.test，免费）"),
    ("江城博物馆（东馆(免费)）", "江城博物馆（东馆(免费)）"),
    ("江城博物馆（东馆）（免费）", "江城博物馆（东馆）（免费）"),
])
def test_only_closed_fee_and_booking_annotations_are_removed(source, expected):
    actual = normalized_place_label(source)
    assert actual == expected
    assert normalized_place_label(actual) == actual
    if actual != source:
        # Output is an ordered subsequence of the original, never new wording.
        original = iter(source)
        assert all(any(letter == candidate for candidate in original) for letter in actual)
