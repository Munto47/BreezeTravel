"""Shared fail-closed contract for the M1-dev synthetic proxy panel."""

from __future__ import annotations

import hashlib
import json
from typing import Any


EVIDENCE_TYPE = "synthetic_proxy"
CALIBRATION_LANE = "M1-dev"
EXPECTED_MODEL = "gpt-5.6-sol"
CONTRACT_VERSION = "auditor-proxy-role-v1"
ROLE_IDS = (
    "proxy-evaluator-1",
    "proxy-evaluator-2",
    "proxy-evaluator-3",
)
PROMPT_VERSION = "m1-dev-auditor-proxy-v1"
GENERATION_REFERENCE_TIME = "2026-08-20T08:00:00+00:00"

_BASE_PROMPT = """You are one independent M1-dev synthetic proxy evaluator for BreezeTravel.
Review only the supplied simulated itinerary, system audit findings, evidence summaries, and repair preview.
Do not use outside facts. Mark the BLOCKER/HIGH categories that the supplied input itself supports.
For every system BLOCKER/HIGH category, decide whether its evidence can be read back from the supplied record.
Record elapsed review seconds and a synthetic repair decision. Never emit human_label, human findings,
real-organizer claims, consent, or human-validation metrics. Your output is synthetic_proxy evidence only.
Do not read or infer another evaluator's output.
"""


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prompt_for(role_id: str) -> str:
    if role_id not in ROLE_IDS:
        raise ValueError(f"unsupported proxy evaluator role: {role_id}")
    return _BASE_PROMPT + f"\nIndependent evaluator slot: {role_id}.\n"


def role_contract(role_id: str) -> dict[str, Any]:
    prompt = prompt_for(role_id)
    return {
        "schema_version": CONTRACT_VERSION,
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": CALIBRATION_LANE,
        "role_id": role_id,
        "execution_status": "NOT_RUN",
        "model": EXPECTED_MODEL,
        "model_version": EXPECTED_MODEL,
        "prompt_version": PROMPT_VERSION,
        "prompt": prompt,
        "prompt_sha256": canonical_sha256(prompt),
        "input_sha256": None,
        "output_sha256": None,
        "generated_at": None,
        "blind": True,
        "independence_contract": {
            "may_read_other_evaluator_outputs": False,
            "may_write_human_fields": False,
            "system_under_test_is_separate": True,
        },
    }


def build_role_contracts() -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_VERSION,
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": CALIBRATION_LANE,
        "generated_at": GENERATION_REFERENCE_TIME,
        "execution_status": "NOT_RUN",
        "human_validated": False,
        "roles": [role_contract(role_id) for role_id in ROLE_IDS],
    }
