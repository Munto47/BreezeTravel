from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.build_release_manifest import build_g07_candidate_manifest


ROOT = Path(__file__).resolve().parents[2]


def test_g07_manifest_is_current_fail_closed_and_secret_free(tmp_path: Path) -> None:
    target = build_g07_candidate_manifest(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == "tc-vnext-g07-candidate-manifest-v1"
    assert payload["goal_id"] == "TC-VNEXT-G07-CANDIDATE"
    assert payload["latest_migration"] == "034_trip_understanding_screenshot_batches.sql"
    assert payload["manifest_generation_executes_tests"] is False
    assert payload["candidate_status"] == "CANDIDATE_EVIDENCE_INCOMPLETE"
    assert payload["candidate_gate_passed"] is False
    assert payload["release_approval_granted"] is False
    assert payload["deployment_requested"] is False
    assert payload["main_merge_requested"] is False
    assert set(payload["gate_status"]) == {f"G{index}" for index in range(9)}
    assert set(payload["gate_status"].values()) == {"NOT_RUN"}
    assert payload["evidence_boundaries"]["human_usability"] == "NOT_RUN"
    assert payload["evidence_boundaries"]["production"] == "NOT_RUN"
    assert set(payload["historical_delivery_receipts"]) == {"G04", "G05", "G06"}
    assert latest["manifest_reference_kind"] == "absolute_external"
    assert latest["manifest"] == str(target.resolve())
    assert latest["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    serialized = target.read_text(encoding="utf-8").lower()
    for forbidden in ("api_key", "authorization: bearer", "blind_truth_payload"):
        assert forbidden not in serialized


def test_g07_run_spec_hash_bindings_match_current_candidate_inputs(tmp_path: Path) -> None:
    payload = json.loads(build_g07_candidate_manifest(tmp_path).read_text(encoding="utf-8"))

    for binding in payload["verified_input_bindings"]:
        path = ROOT / binding["path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == binding["sha256"]
    assert any(
        binding["binding"] == "automated_candidate_gate"
        for binding in payload["verified_input_bindings"]
    )
    assert any(
        binding["binding"] == "text_card_90_case_contract"
        for binding in payload["verified_input_bindings"]
    )
