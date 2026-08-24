"""Single fail-closed switch for formal P5 evidence contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


P5_ROOT = Path(__file__).resolve().parent
ACTIVE_CONTRACT_PATH = P5_ROOT / "active_contract.json"
SOURCE_V2_CONTRACT_PATH = P5_ROOT / "source_active_contract_v2.json"
V2_CONTRACT_ID = "trip-check-p5-v2"
V3_CONTRACT_ID = "trip-check-p5-v3"
V4_CONTRACT_ID = "trip-check-p5-v4"
V5_CONTRACT_ID = "trip-check-p5-v5"
V1_CONTRACT_ID = "trip-check-p5-v1"


class P5ContractNotReadyError(RuntimeError):
    pass


def load_active_contract(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "trip-check-p5-active-contract-v1":
        raise P5ContractNotReadyError("P5_ACTIVE_CONTRACT_INVALID")
    return payload


def require_v2_formal_ready(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    payload = load_active_contract(path)
    if payload.get("active_contract") in {
        V3_CONTRACT_ID,
        V4_CONTRACT_ID,
        V5_CONTRACT_ID,
    }:
        raise P5ContractNotReadyError("P5_V2_FORMAL_CONTRACT_SUPERSEDED")
    if payload.get("active_contract") != V2_CONTRACT_ID or payload.get("formal_evidence_status") != "READY":
        raise P5ContractNotReadyError("P5_V2_FORMAL_CONTRACT_NOT_READY")
    return payload


def require_v3_formal_ready(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    payload = load_active_contract(path)
    if payload.get("active_contract") in {V4_CONTRACT_ID, V5_CONTRACT_ID}:
        raise P5ContractNotReadyError("P5_V3_FORMAL_CONTRACT_SUPERSEDED")
    if payload.get("active_contract") != V3_CONTRACT_ID or payload.get("formal_evidence_status") != "READY":
        raise P5ContractNotReadyError("P5_V3_FORMAL_CONTRACT_NOT_READY")
    return payload


def require_v4_formal_ready(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    payload = load_active_contract(path)
    if payload.get("active_contract") == V5_CONTRACT_ID:
        raise P5ContractNotReadyError("P5_V4_FORMAL_CONTRACT_SUPERSEDED")
    source = payload.get("source_v3_contract")
    seal_hash = payload.get("blind_seal_v4_sha256")
    if (
        payload.get("active_contract") != V4_CONTRACT_ID
        or payload.get("formal_evidence_status") != "READY"
        or not isinstance(seal_hash, str)
        or len(seal_hash) != 64
        or any(character not in "0123456789abcdef" for character in seal_hash)
        or not isinstance(source, dict)
        or source.get("active_contract") != V3_CONTRACT_ID
        or source.get("formal_evidence_status") != "READY"
    ):
        raise P5ContractNotReadyError("P5_V4_FORMAL_CONTRACT_NOT_READY")
    return payload


def require_v5_formal_ready(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    payload = load_active_contract(path)
    source = payload.get("source_v4_contract")
    seal_hash = payload.get("blind_seal_v5_sha256")
    if (
        payload.get("active_contract") != V5_CONTRACT_ID
        or payload.get("formal_evidence_status") != "READY"
        or not isinstance(seal_hash, str)
        or len(seal_hash) != 64
        or any(character not in "0123456789abcdef" for character in seal_hash)
        or not isinstance(source, dict)
        or source.get("active_contract") != V4_CONTRACT_ID
        or source.get("formal_evidence_status") != "READY"
    ):
        raise P5ContractNotReadyError("P5_V5_FORMAL_CONTRACT_NOT_READY")
    return payload


def reject_v1_formal(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    """Permanently reject v1 formal evidence after its supersession receipt exists."""

    payload = load_active_contract(path)
    deprecated = payload.get("deprecated_contracts")
    if not isinstance(deprecated, list):
        raise P5ContractNotReadyError("P5_ACTIVE_CONTRACT_INVALID")
    for item in deprecated:
        if (
            isinstance(item, dict)
            and item.get("contract_id") == V1_CONTRACT_ID
            and item.get("formal_evidence_eligible") is False
        ):
            raise P5ContractNotReadyError("P5_V1_FORMAL_CONTRACT_SUPERSEDED")
    if payload.get("active_contract") != V1_CONTRACT_ID:
        raise P5ContractNotReadyError("P5_V1_FORMAL_CONTRACT_INACTIVE")
    return payload
