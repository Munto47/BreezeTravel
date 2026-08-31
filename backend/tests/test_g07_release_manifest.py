from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from evals.agent_gate_v1.core_gate import CORE_CONFIG_ROOTS, CORE_DATA_ROOTS
from scripts import build_release_manifest
from scripts.build_release_manifest import build_g07_candidate_manifest


ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _component_receipts(root: Path) -> list[Path]:
    commit = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(ROOT), "show", "-s", "--format=%T", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config_sha256 = build_release_manifest._g07_git_bundle_sha256(
        commit, CORE_CONFIG_ROOTS
    )
    data_sha256 = build_release_manifest._g07_git_bundle_sha256(commit, CORE_DATA_ROOTS)
    binding = json.loads(
        (ROOT / "docs/governance/current_goal_binding.json").read_text(
            encoding="utf-8"
        )
    )
    levels = {
        "AUTOMATED_PRODUCT_GATE": "AUTOMATED_TEST",
        "LIVE_PROVIDER_GATE": "LIVE_PROVIDER_EVIDENCE",
        "MULTI_AGENT_PANEL": "MULTI_AGENT_SIMULATED_REVIEW",
        "SEALED_AGENT_BLIND": "SEALED_AGENT_BLIND",
    }
    paths: list[Path] = []
    for index, (component, evidence_level) in enumerate(levels.items(), start=1):
        path = root / f"component-{index}.json"
        _write_json(
            path,
            {
                "candidate_commit": commit,
                "candidate_tree": tree,
                "candidate_config_sha256": config_sha256,
                "candidate_data_sha256": data_sha256,
                "automated_gate_contract_sha256": binding[
                    "candidate_gate_contract_sha256"
                ],
                "component": component,
                "evidence_level": evidence_level,
                "upstream_artifact_sha256": {
                    f"component_{index}.evidence": str(index) * 64
                },
                "verifier_sha256": "f" * 64,
                "isolation_mode": (
                    "FRESH_CLEAN_CHECKOUT"
                    if component == "AUTOMATED_PRODUCT_GATE"
                    else None
                ),
            },
        )
        paths.append(path)
    return paths


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


def test_g07_manifest_aggregates_only_complete_same_subject_components(
    tmp_path: Path,
) -> None:
    components = _component_receipts(tmp_path)

    target = build_g07_candidate_manifest(
        tmp_path / "manifest",
        component_receipt_paths=components,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["manifest_gate_status"] == "PASS"
    assert payload["candidate_gate_passed"] is False
    assert payload["candidate_status"] == "CANDIDATE_EVIDENCE_INCOMPLETE"
    assert set(payload["component_receipts"]) == build_release_manifest.G07_COMPONENTS
    assert set(payload["component_receipt_sha256"]) == (
        build_release_manifest.G07_COMPONENTS
    )
    assert "G07_COMPONENT_RECEIPTS_NOT_RUN" not in payload["release_blockers"]
    assert payload["evidence_boundaries"]["live_provider"] == (
        "VERIFIED_COMPONENT_RECEIPT"
    )
    assert payload["evidence_boundaries"]["multi_agent"] == (
        "VERIFIED_COMPONENT_RECEIPT"
    )
    assert payload["evidence_boundaries"]["sealed_blind"] == (
        "VERIFIED_COMPONENT_RECEIPT"
    )
    for path in components:
        component = json.loads(path.read_text(encoding="utf-8"))["component"]
        assert payload["component_receipt_sha256"][component] == hashlib.sha256(
            path.read_bytes()
        ).hexdigest()


def test_g07_manifest_rejects_partial_duplicate_and_cross_subject_components(
    tmp_path: Path,
) -> None:
    components = _component_receipts(tmp_path)

    with pytest.raises(RuntimeError, match="exactly four"):
        build_g07_candidate_manifest(
            tmp_path / "partial",
            component_receipt_paths=components[:3],
        )
    with pytest.raises(RuntimeError, match="duplicate"):
        build_g07_candidate_manifest(
            tmp_path / "duplicate",
            component_receipt_paths=[components[0], components[0], *components[2:]],
        )

    drifted = json.loads(components[0].read_text(encoding="utf-8"))
    drifted["candidate_commit"] = "0" * 40
    _write_json(components[0], drifted)
    with pytest.raises(RuntimeError, match="binding mismatch"):
        build_g07_candidate_manifest(
            tmp_path / "drifted",
            component_receipt_paths=components,
        )


def test_g07_candidate_contract_receipt_binds_subject_git_bytes() -> None:
    receipt = json.loads(
        (
            ROOT / "docs/governance/gate-results/G07.candidate-contract.json"
        ).read_text(encoding="utf-8")
    )
    subject = receipt["subject_commit"]
    tree = subprocess.run(
        ["git", "-C", str(ROOT), "show", "-s", "--format=%T", subject],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert tree == receipt["subject_tree"]
    assert receipt["remote_subject"] == subject
    assert receipt["verdict"] == "G07_CANDIDATE_CONTRACT_FROZEN"
    for path, expected_sha256 in receipt["artifact_sha256"].items():
        content = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{subject}:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        assert hashlib.sha256(content).hexdigest() == expected_sha256
