from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import file_sha256
from scripts import build_trip_check_p6_real_ocr_dataset as builder


def _field(index: int, *, parent: bool) -> dict:
    row = index // 3
    column = index % 3
    return {
        "box": [column * 110, row * 30, column * 110 + 100, row * 30 + 20],
        "color": "rendered-line" if parent else "block-rendered-line",
        "field_type": "PLACE",
        "font_size": 16,
        "text": f"parent-{index}" if index < 3 else f"extra-{index}",
    }


def _contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict, Path]:
    viewport = {
        "dom_width": 1280,
        "dom_height": 720,
        "captured_width": 1265,
        "captured_height": 712,
    }
    parent_cities = []
    dom_cities = []
    for city in builder.CITY_CONFIG:
        parent_items = []
        dom_items = []
        for index in range(20):
            metadata = {
                "item_id": f"ocr-{city}-{index:02d}",
                "city": city,
                "scroll_y": index * 10,
                "scroll_height": 1000,
            }
            parent_items.append({**metadata, "fields": [_field(i, parent=True) for i in range(3)]})
            dom_items.append({**metadata, "fields": [_field(i, parent=False) for i in range(6)]})
        city_metadata = {
            "city": city,
            "url": f"https://example.test/{city}",
            "title": city,
            "step": 1,
        }
        parent_cities.append({**city_metadata, "items": parent_items})
        dom_cities.append({**city_metadata, "items": dom_items})
    parent = {
        "schema_version": builder.PARENT_ANNOTATION_SCHEMA,
        "annotation_unit": builder.PARENT_ANNOTATION_UNIT,
        "viewport": viewport,
        "cities": parent_cities,
    }
    parent_path = tmp_path / "parent.json"
    parent_path.write_text(json.dumps(parent), encoding="utf-8")
    parent_sha = file_sha256(parent_path)
    monkeypatch.setattr(builder, "PARENT_ANNOTATION_SHA256", parent_sha)
    monkeypatch.setattr(builder, "PARENT_FIELD_COUNT", 180)
    dom = {
        "schema_version": builder.DOM_ANNOTATION_SCHEMA,
        "annotation_unit": builder.ANNOTATION_UNIT,
        "viewport": viewport,
        "captured_at": "2026-01-01T00:00:00Z",
        "generated_at": "2026-01-01T00:01:00Z",
        "source_image_set": builder.SOURCE_IMAGE_SET,
        "source_annotation_file_sha256": parent_sha,
        "candidate_annotation_file_sha256": builder.CANDIDATE_ANNOTATION_SHA256,
        "selection_policy": dict(builder.SELECTION_POLICY),
        "cities": dom_cities,
    }
    return dom, parent, parent_path


def test_parent_binding_requires_complete_frozen_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom, parent, parent_path = _contracts(tmp_path, monkeypatch)

    result = builder._validate_parent_binding(dom, parent, parent_path)

    assert result == {
        "parent_annotation_file_sha256": file_sha256(parent_path),
        "parent_annotation_field_count": 180,
        "parent_annotation_bound_count": 180,
        "annotation_field_count": 360,
    }


def test_parent_binding_rejects_one_unbound_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom, parent, parent_path = _contracts(tmp_path, monkeypatch)
    dom["cities"][0]["items"][0]["fields"][0]["text"] = "different-place"

    with pytest.raises(
        builder.DatasetBuildError,
        match="does not bind every frozen parent field",
    ):
        builder._validate_parent_binding(dom, parent, parent_path)


def test_parent_binding_rejects_ocr_informed_selection_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom, parent, parent_path = _contracts(tmp_path, monkeypatch)
    dom["selection_policy"]["ocr_output_used"] = True

    with pytest.raises(
        builder.DatasetBuildError,
        match="parent-preserving annotation source binding is invalid",
    ):
        builder._validate_parent_binding(dom, parent, parent_path)
