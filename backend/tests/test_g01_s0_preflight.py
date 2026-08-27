from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from evals.trip_nlu_v2.validator import (
    DatasetValidationError,
    _current_code_bindings,
    validate_dataset,
)
from scripts.check_g01_s0 import audit_inventory


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_nlu_v2"
CANDIDATE_MANIFEST = (
    BACKEND_ROOT / "eval_data" / "trip_nlu_v2_remediation" / "candidate_manifest.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _newline_hashes(path: Path) -> set[str]:
    raw = path.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    return {hashlib.sha256(value).hexdigest() for value in (raw, lf, crlf)}


def test_every_s0_asset_has_exactly_one_disposition_and_frozen_assets_are_unchanged() -> None:
    receipt = audit_inventory()

    assert receipt["status"] == "PASS"
    assert receipt["legacy_openapi"] == {
        "sha256": "0a616cf711b260a232d20aca80d6904743327ff9dcbc2808356c62066fc55a81",
        "path_count": 99,
        "operation_count": 106,
    }
    assert receipt["frozen_diff"] == []


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None, reason="FastAPI runtime not installed")
def test_current_source_keeps_every_legacy_openapi_path_and_method() -> None:
    from app.main import app

    legacy = json.loads(
        (BACKEND_ROOT.parent / "packages" / "trip-check-client" / "openapi.json").read_text(
            encoding="utf-8"
        )
    )
    current = app.openapi()
    methods = {"get", "post", "put", "patch", "delete"}
    missing = [
        (path, method)
        for path, item in legacy["paths"].items()
        for method in item
        if method in methods and method not in current["paths"].get(path, {})
    ]
    assert missing == []
    assert {
        "/api/rooms/{room_id}/trip-intakes",
        "/api/rooms/{room_id}/trip-intakes/latest",
        "/api/rooms/{room_id}/trip-intakes/screenshots",
        "/api/trip-intakes/{intake_id}/revisions/{revision}",
        "/api/trip-intakes/{intake_id}/revisions/{revision}/confirm",
        "/api/trip-intakes/{intake_id}/revisions/{revision}/materialize",
    }.issubset(current["paths"])


def test_historical_candidate_binding_is_invalid_without_mutating_frozen_assets(
    tmp_path: Path,
) -> None:
    manifest = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    assert sum(
        _sha256(DATA_ROOT / relative) == expected
        for relative, expected in manifest["files"].items()
    ) == len(manifest["files"]) == 10

    current = _current_code_bindings(BACKEND_ROOT)
    assert current["schema_sha256"] == manifest["code_bindings"]["schema_sha256"]
    assert manifest["code_bindings"]["generator_sha256"] in _newline_hashes(
        BACKEND_ROOT / "scripts" / "generate_trip_nlu_v2.py"
    )
    for name, relative in {
        "validator_sha256": "evals/trip_nlu_v2/validator.py",
        "scorer_sha256": "evals/trip_nlu_v2/scorer.py",
        "gate_sha256": "evals/trip_nlu_v2/gate.py",
    }.items():
        assert manifest["code_bindings"][name] not in _newline_hashes(BACKEND_ROOT / relative)

    diagnostic_manifest = tmp_path / "candidate_manifest.current-bindings.json"
    manifest["code_bindings"] = current
    diagnostic_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    receipt = validate_dataset(DATA_ROOT, manifest_path=diagnostic_manifest)
    assert receipt["valid"] is True
    assert receipt["case_count"] == 120
    assert receipt["blind_labels_read"] is False

    with pytest.raises(DatasetValidationError, match="outside the repository"):
        validate_dataset(
            DATA_ROOT,
            external_blind_labels=DATA_ROOT / "frozen_blind.inputs.jsonl",
            manifest_path=diagnostic_manifest,
        )
