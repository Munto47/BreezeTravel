"""Single fail-closed switch for formal P5 evidence contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


P5_ROOT = Path(__file__).resolve().parent
ACTIVE_CONTRACT_PATH = P5_ROOT / "active_contract.json"
V2_CONTRACT_ID = "trip-check-p5-v2"


class P5ContractNotReadyError(RuntimeError):
    pass


def load_active_contract(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "trip-check-p5-active-contract-v1":
        raise P5ContractNotReadyError("P5_ACTIVE_CONTRACT_INVALID")
    return payload


def require_v2_formal_ready(path: Path = ACTIVE_CONTRACT_PATH) -> dict[str, Any]:
    payload = load_active_contract(path)
    if (
        payload.get("active_contract") != V2_CONTRACT_ID
        or payload.get("formal_evidence_status") != "READY"
    ):
        raise P5ContractNotReadyError("P5_V2_FORMAL_CONTRACT_NOT_READY")
    return payload
