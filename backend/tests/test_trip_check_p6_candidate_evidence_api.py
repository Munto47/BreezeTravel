from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import evidence as evidence_api
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest


SUBJECT = "a" * 40
MANIFEST_HASH = "b" * 64
RUN_SPEC_HASH = "d" * 64
UPSTREAM_REF = "origin/codex/trip-check-p6-candidate-evidence"
SCOPE = {
    "cities": ["北京", "上海", "杭州"],
    "single_city": True,
    "group_size": {"min": 2, "max": 5},
    "trip_days": {"min": 2, "max": 5},
    "input_types": ["TEXT", "SCREENSHOT"],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(tmp_path: Path) -> tuple[Path, Path]:
    evidence = tmp_path / "candidate-evidence.json"
    release = tmp_path / "release-manifest.json"
    evidence.write_text("{}", encoding="utf-8")
    release.write_text("{}", encoding="utf-8")
    return evidence, release


def _settings(evidence: Path, release: Path, **overrides):
    values = {
        "candidate_evidence_path": str(evidence.resolve()),
        "candidate_evidence_sha256": _sha256(evidence),
        "candidate_release_manifest_path": str(release.resolve()),
        "candidate_release_manifest_sha256": _sha256(release),
        "candidate_gate_receipt_path": "",
        "candidate_gate_receipt_sha256": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _payloads() -> tuple[dict, dict]:
    public = {
        "subject_commit": SUBJECT,
        "manifest_hash": MANIFEST_HASH,
        "scope": SCOPE,
        "known_gaps": ["HUMAN_EVIDENCE_NOT_RUN"],
        "public_e2e": {"url": "https://www.breezetravel.cn"},
        "human_evidence": False,
        "gates": {f"g{index}": "PASS" for index in range(6)} | {"g6": "NOT_RUN"},
        "candidate_gate_status": "NOT_RUN",
        "candidate_gate_receipt_hash": None,
    }
    release = {
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM_REF,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "candidate_run_spec_hash": RUN_SPEC_HASH,
        "manifest_hash": MANIFEST_HASH,
        "scope": SCOPE,
        "known_gaps": ["HUMAN_EVIDENCE_NOT_RUN"],
        "public_e2e": {"url": "https://www.breezetravel.cn"},
        "human_evidence": False,
        "read_only_mount": True,
        "gates": {f"g{index}": {"status": "PASS"} for index in range(6)},
    }
    return public, release


def _candidate_gate_receipt(*, run_spec_hash: str = RUN_SPEC_HASH) -> dict:
    payload = {
        "schema_version": "trip-check-p6-candidate-gate-receipt-v1",
        "subject_commit": SUBJECT,
        "upstream_ref": UPSTREAM_REF,
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "run_spec_hash": run_spec_hash,
        "manifest_hash": MANIFEST_HASH,
        "pre_gate_evidence_sha256": "1" * 64,
        "pre_gate_evidence_response_body_sha256": "2" * 64,
        "pre_gate_health_response_body_sha256": "3" * 64,
        "g6_readback_receipt_hash": "4" * 64,
        "decision": "PASS",
        "decided_at": "2026-08-25T00:02:00Z",
    }
    payload["receipt_hash"] = digest(payload)
    return payload


def test_configured_candidate_evidence_is_hash_bound_and_returned(tmp_path, monkeypatch):
    evidence_path, release_path = _files(tmp_path)
    public, release = _payloads()
    monkeypatch.setattr(evidence_api, "get_settings", lambda: _settings(evidence_path, release_path))
    monkeypatch.setattr(
        evidence_api,
        "validate_candidate_evidence",
        lambda _payload: public,
    )
    monkeypatch.setattr(evidence_api, "validate_release_manifest", lambda _payload: release)

    assert asyncio.run(evidence_api.latest_evidence()) == public


@pytest.mark.parametrize("override", [
    {"candidate_evidence_sha256": "0" * 64},
    {"candidate_release_manifest_sha256": "0" * 64},
    {"candidate_release_manifest_path": ""},
])
def test_configured_candidate_evidence_fails_closed_on_hash_or_partial_config(
    tmp_path, monkeypatch, override,
):
    evidence_path, release_path = _files(tmp_path)
    monkeypatch.setattr(
        evidence_api,
        "get_settings",
        lambda: _settings(evidence_path, release_path, **override),
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(evidence_api.latest_evidence())
    assert raised.value.status_code == 503
    assert raised.value.detail == "CANDIDATE_EVIDENCE_UNAVAILABLE"


def test_configured_candidate_evidence_fails_closed_on_schema_or_cross_binding(
    tmp_path, monkeypatch,
):
    evidence_path, release_path = _files(tmp_path)
    monkeypatch.setattr(evidence_api, "get_settings", lambda: _settings(evidence_path, release_path))
    monkeypatch.setattr(
        evidence_api,
        "validate_candidate_evidence",
        lambda _payload: (_ for _ in ()).throw(P6ContractError("INVALID")),
    )
    with pytest.raises(HTTPException, match="503"):
        asyncio.run(evidence_api.latest_evidence())


def test_candidate_evidence_must_be_external_and_use_distinct_files(tmp_path, monkeypatch):
    evidence_path, release_path = _files(tmp_path)
    internal = evidence_api._LEGACY_MANIFEST
    monkeypatch.setattr(
        evidence_api,
        "get_settings",
        lambda: _settings(internal, release_path),
    )
    with pytest.raises(HTTPException, match="503"):
        asyncio.run(evidence_api.latest_evidence())

    monkeypatch.setattr(
        evidence_api,
        "get_settings",
        lambda: _settings(evidence_path, evidence_path),
    )
    with pytest.raises(HTTPException, match="503"):
        asyncio.run(evidence_api.latest_evidence())

    public, release = _payloads()
    release["subject_commit"] = "f" * 40
    monkeypatch.setattr(evidence_api, "get_settings", lambda: _settings(evidence_path, release_path))
    monkeypatch.setattr(
        evidence_api,
        "validate_candidate_evidence",
        lambda _payload: public,
    )
    monkeypatch.setattr(evidence_api, "validate_release_manifest", lambda _payload: release)
    with pytest.raises(HTTPException, match="503"):
        asyncio.run(evidence_api.latest_evidence())


def test_candidate_gate_pass_requires_bound_external_receipt(tmp_path, monkeypatch):
    evidence_path, release_path = _files(tmp_path)
    receipt_path = tmp_path / "candidate-gate-receipt.json"
    receipt = _candidate_gate_receipt()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    public, release = _payloads()
    public["gates"]["g6"] = "PASS"
    public["candidate_gate_status"] = "PASS"
    public["candidate_gate_receipt_hash"] = receipt["receipt_hash"]
    monkeypatch.setattr(evidence_api, "validate_candidate_evidence", lambda _payload: public)
    monkeypatch.setattr(evidence_api, "validate_release_manifest", lambda _payload: release)
    monkeypatch.setattr(evidence_api, "get_settings", lambda: _settings(evidence_path, release_path))
    with pytest.raises(HTTPException, match="503"):
        asyncio.run(evidence_api.latest_evidence())

    monkeypatch.setattr(evidence_api, "get_settings", lambda: _settings(
        evidence_path,
        release_path,
        candidate_gate_receipt_path=str(receipt_path.resolve()),
        candidate_gate_receipt_sha256=_sha256(receipt_path),
    ))
    assert asyncio.run(evidence_api.latest_evidence()) == public

    mismatched_receipt = _candidate_gate_receipt(run_spec_hash="e" * 64)
    receipt_path.write_text(json.dumps(mismatched_receipt), encoding="utf-8")
    public["candidate_gate_receipt_hash"] = mismatched_receipt["receipt_hash"]
    monkeypatch.setattr(evidence_api, "get_settings", lambda: _settings(
        evidence_path,
        release_path,
        candidate_gate_receipt_path=str(receipt_path.resolve()),
        candidate_gate_receipt_sha256=_sha256(receipt_path),
    ))
    with pytest.raises(HTTPException, match="503"):
        asyncio.run(evidence_api.latest_evidence())


@pytest.mark.parametrize(
    ("receipt_path", "receipt_sha256"),
    [("receipt.json", ""), ("", "f" * 64), ("receipt.json", "f" * 64)],
)
def test_receipt_only_configuration_never_falls_back_to_legacy(
    tmp_path, monkeypatch, receipt_path, receipt_sha256,
):
    blank_core = SimpleNamespace(
        candidate_evidence_path="",
        candidate_evidence_sha256="",
        candidate_release_manifest_path="",
        candidate_release_manifest_sha256="",
        candidate_gate_receipt_path=str(tmp_path / receipt_path) if receipt_path else "",
        candidate_gate_receipt_sha256=receipt_sha256,
    )
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"status":"legacy"}', encoding="utf-8")
    monkeypatch.setattr(evidence_api, "get_settings", lambda: blank_core)
    monkeypatch.setattr(evidence_api, "_LEGACY_MANIFEST", legacy)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(evidence_api.latest_evidence())
    assert raised.value.status_code == 503


def test_unconfigured_candidate_uses_legacy_and_missing_legacy_is_404(tmp_path, monkeypatch):
    blank = SimpleNamespace(
        candidate_evidence_path="",
        candidate_evidence_sha256="",
        candidate_release_manifest_path="",
        candidate_release_manifest_sha256="",
        candidate_gate_receipt_path="",
        candidate_gate_receipt_sha256="",
    )
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"status":"legacy"}', encoding="utf-8")
    monkeypatch.setattr(evidence_api, "get_settings", lambda: blank)
    monkeypatch.setattr(evidence_api, "_LEGACY_MANIFEST", legacy)
    assert asyncio.run(evidence_api.latest_evidence()) == {"status": "legacy"}
    monkeypatch.setattr(evidence_api, "_LEGACY_MANIFEST", tmp_path / "missing.json")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(evidence_api.latest_evidence())
    assert raised.value.status_code == 404
