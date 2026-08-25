"""Read-only, deploy-safe access to historical or P6 candidate evidence."""

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    validate_candidate_gate_receipt,
    validate_candidate_evidence,
    validate_release_manifest,
)

router = APIRouter()

_LEGACY_MANIFEST = Path(__file__).resolve().parents[2] / "evidence" / "latest.json"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_UNAVAILABLE = "CANDIDATE_EVIDENCE_UNAVAILABLE"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    return payload


def _read_bound_json(path: Path, expected_sha256: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE)
        payload = json.loads(raw.decode("utf-8"))
    except HTTPException:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    return payload


def _external_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(_REPO_ROOT)
    except ValueError:
        return resolved
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    raise HTTPException(status_code=503, detail=_UNAVAILABLE)


def _candidate_evidence() -> dict[str, Any] | None:
    settings = get_settings()
    configured = (
        settings.candidate_evidence_path,
        settings.candidate_evidence_sha256,
        settings.candidate_release_manifest_path,
        settings.candidate_release_manifest_sha256,
    )
    receipt_config = (
        settings.candidate_gate_receipt_path,
        settings.candidate_gate_receipt_sha256,
    )
    if not any(configured + receipt_config):
        return None
    if not all(configured) or (any(receipt_config) and not all(receipt_config)):
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    evidence_path = _external_path(settings.candidate_evidence_path)
    release_path = _external_path(settings.candidate_release_manifest_path)
    if evidence_path == release_path:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    try:
        public = validate_candidate_evidence(
            _read_bound_json(evidence_path, settings.candidate_evidence_sha256)
        )
        release = validate_release_manifest(
            _read_bound_json(release_path, settings.candidate_release_manifest_sha256)
        )
    except P6ContractError as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    if not (
        public["subject_commit"] == release["subject_commit"]
        and public["manifest_hash"] == release["manifest_hash"]
        and public["scope"] == release["scope"]
        and public["known_gaps"] == release["known_gaps"]
        and public["public_e2e"]["url"] == release["public_e2e"]["url"]
        and public["human_evidence"] is False
        and release["human_evidence"] is False
        and release["read_only_mount"] is True
        and all(
            public["gates"][gate] == release["gates"][gate]["status"]
            for gate in ("g0", "g1", "g2", "g3", "g4", "g5")
        )
    ):
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    if public["candidate_gate_status"] == "PASS":
        if not all(receipt_config):
            raise HTTPException(status_code=503, detail=_UNAVAILABLE)
        receipt_path = _external_path(settings.candidate_gate_receipt_path)
        if receipt_path in {evidence_path, release_path}:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE)
        try:
            receipt = validate_candidate_gate_receipt(
                _read_bound_json(receipt_path, settings.candidate_gate_receipt_sha256)
            )
        except P6ContractError as exc:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
        if not (
            public["candidate_gate_receipt_hash"] == receipt["receipt_hash"]
            and public["subject_commit"] == receipt["subject_commit"]
            and public["manifest_hash"] == receipt["manifest_hash"]
            and receipt["run_spec_hash"] == release["candidate_run_spec_hash"]
            and receipt["upstream_ref"] == release["upstream_ref"]
            and receipt["upstream_commit"] == release["upstream_commit"]
            and receipt["dirty_tree"] is False
        ):
            raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    elif any(receipt_config):
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    return public


@router.get("/evidence/latest")
async def latest_evidence():
    """Return the public evidence manifest; no raw prompts, keys, or user data."""
    candidate = _candidate_evidence()
    if candidate is not None:
        return candidate
    if not _LEGACY_MANIFEST.exists():
        raise HTTPException(status_code=404, detail="尚未发布评测证据")
    return _read_json(_LEGACY_MANIFEST)
