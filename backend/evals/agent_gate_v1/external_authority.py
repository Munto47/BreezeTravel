from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from evals.agent_gate_v1.contracts import (
    AgentGateAuthorityManifest,
    DetachedAuthoritySignature,
    StrictModel,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.path_security import read_external_snapshot
from evals.agent_gate_v1.signing import unsigned_payload, verify_payload_signature


class ExternalAuthorityError(ValueError):
    pass


PROTOCOL_CONTRACT_PATH = (
    "backend/eval_data/agent_gate_v1/protocol_contract.json"
)
PROTOCOL_SCHEMA_ROOT = "backend/eval_data/agent_gate_v1"
REQUIRED_EXTERNAL_AUTHORITY_SCHEMAS = {
    "external_signer_conformance_receipt.schema.json"
}
MAX_PROTOCOL_ARTIFACT_BYTES = 1_000_000
MAX_PROTOCOL_SCHEMA_BINDINGS = 128
MAX_INTEROPERABLE_INTEGER = 9_007_199_254_740_991
MAX_FLOAT_TOKEN_LENGTH = 128


def _git_blob(
    repository_root: Path,
    candidate_commit: str,
    repository_path: str,
) -> bytes:
    if re.fullmatch(r"[0-9a-f]{40}", candidate_commit) is None:
        raise ExternalAuthorityError("candidate commit is invalid")
    root = repository_root.resolve(strict=True)
    subject = f"{candidate_commit}:{repository_path}"
    try:
        size_result = subprocess.run(
            [
                trusted_host_tool("git"),
                "-C",
                str(root),
                "cat-file",
                "-s",
                subject,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalAuthorityError("candidate Git readback timed out") from exc
    if size_result.returncode != 0:
        raise ExternalAuthorityError("candidate Git readback failed")
    try:
        size = int(size_result.stdout.strip())
    except ValueError as exc:
        raise ExternalAuthorityError("candidate Git size readback is invalid") from exc
    if not 0 <= size <= MAX_PROTOCOL_ARTIFACT_BYTES:
        raise ExternalAuthorityError("candidate protocol artifact exceeds size limit")
    try:
        result = subprocess.run(
            [
                trusted_host_tool("git"),
                "-C",
                str(root),
                "show",
                subject,
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalAuthorityError("candidate Git readback timed out") from exc
    if result.returncode != 0 or len(result.stdout) != size:
        raise ExternalAuthorityError("candidate Git readback failed")
    return result.stdout


def _strict_json_object(content: bytes) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ExternalAuthorityError("protocol contract has duplicate keys")
            value[key] = item
        return value

    def reject_non_finite_constant(value: str) -> None:
        raise ExternalAuthorityError(
            f"protocol JSON contains a non-standard constant: {value}"
        )

    def parse_interoperable_integer(value: str) -> int:
        digits = value.removeprefix("-")
        if len(digits) > 16:
            raise ExternalAuthorityError(
                "protocol JSON integer exceeds the interoperable range"
            )
        parsed = int(value)
        if abs(parsed) > MAX_INTEROPERABLE_INTEGER:
            raise ExternalAuthorityError(
                "protocol JSON integer exceeds the interoperable range"
            )
        return parsed

    def parse_interoperable_float(value: str) -> float:
        if len(value) > MAX_FLOAT_TOKEN_LENGTH:
            raise ExternalAuthorityError(
                "protocol JSON number exceeds the interoperable range"
            )
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ExternalAuthorityError(
                "protocol JSON number exceeds the interoperable range"
            )
        if parsed == 0.0:
            mantissa = value.lower().split("e", maxsplit=1)[0]
            significant_digits = mantissa.lstrip("+-").replace(".", "")
            if any(digit != "0" for digit in significant_digits):
                raise ExternalAuthorityError(
                    "protocol JSON number underflows the interoperable range"
                )
        return parsed

    try:
        value = json.loads(
            content,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite_constant,
            parse_float=parse_interoperable_float,
            parse_int=parse_interoperable_integer,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalAuthorityError("protocol contract is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ExternalAuthorityError("protocol contract must be an object")
    return value


def verify_candidate_protocol_schema_bindings(
    *,
    repository_root: Path,
    candidate_commit: str,
) -> str:
    """Fail closed when a checked-in protocol schema drifts from its Git contract."""

    contract_bytes = _git_blob(
        repository_root,
        candidate_commit,
        PROTOCOL_CONTRACT_PATH,
    )
    contract = _strict_json_object(contract_bytes)
    bindings = contract.get("schema_sha256")
    if not isinstance(bindings, dict) or not bindings:
        raise ExternalAuthorityError("protocol contract has no schema bindings")
    if len(bindings) > MAX_PROTOCOL_SCHEMA_BINDINGS:
        raise ExternalAuthorityError("protocol contract has too many schema bindings")
    if not REQUIRED_EXTERNAL_AUTHORITY_SCHEMAS.issubset(bindings):
        raise ExternalAuthorityError("external authority schema binding is absent")
    for filename, expected_sha256 in bindings.items():
        if (
            not isinstance(filename, str)
            or re.fullmatch(r"[a-z0-9_]+\.schema\.json", filename) is None
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise ExternalAuthorityError("protocol schema binding is invalid")
        schema_bytes = _git_blob(
            repository_root,
            candidate_commit,
            f"{PROTOCOL_SCHEMA_ROOT}/{filename}",
        )
        if hashlib.sha256(schema_bytes).hexdigest() != expected_sha256:
            raise ExternalAuthorityError(
                f"protocol schema hash mismatch: {filename}"
            )
        schema = _strict_json_object(schema_bytes)
        if schema.get("type") != "object" or not isinstance(
            schema.get("properties"), dict
        ):
            raise ExternalAuthorityError(f"protocol schema is invalid: {filename}")
    return hashlib.sha256(contract_bytes).hexdigest()


class ExternalSignerConformanceReceipt(StrictModel):
    """Authority-owned broker proof; candidate code never constructs this payload."""

    schema_version: Literal["external-signer-conformance-receipt-v1"] = (
        "external-signer-conformance-receipt-v1"
    )
    broker_id: str = Field(pattern=r"^BROKER-[A-Z0-9-]{8,100}$")
    signer_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supervisor_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    broker_registry_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    broker_registry_path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_role_bindings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    authority_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    conformance_run_id: str = Field(pattern=r"^SIGCONF-[0-9A-F]{32}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    challenge_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supported_operations: list[
        Literal[
            "PREPARE_ACTIVATION",
            "MINT_LIVE",
            "CAPTURE_AMAP_EFFECT",
            "CAPTURE_QWEN_EFFECT",
            "SIGN_AUTOMATED_COMPONENT",
            "SIGN_LIVE_COMPONENT",
            "SIGN_PANEL_COMPONENT",
            "SIGN_SEALED_COMPONENT",
            "SIGN_FINAL_GATE",
        ]
    ] = Field(min_length=9, max_length=9)
    two_stage: Literal[True] = True
    exact_retry_idempotent: Literal[True] = True
    conflicting_request_rejected: Literal[True] = True
    expired_request_rejected: Literal[True] = True
    replay_rejected: Literal[True] = True
    tamper_rejected: Literal[True] = True
    caller_payload_accepted: Literal[False] = False
    caller_role_accepted: Literal[False] = False
    caller_verdict_accepted: Literal[False] = False
    generic_signing_endpoint: Literal[False] = False
    purpose_specific_payload_construction: Literal[True] = True
    authority_supervisor_separate: Literal[True] = True
    role_process_count: Literal[8] = 8
    role_private_key_count: Literal[8] = 8
    shared_private_key_process: Literal[False] = False
    sanitized_environment_keys_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inherited_handle_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    child_process_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_exit_code: Literal[0] = 0
    timed_out: Literal[False] = False
    signer_network_access: Literal[False] = False
    repository_external: Literal[True] = True
    imports_candidate_code: Literal[False] = False
    private_key_in_candidate_process: Literal[False] = False
    key_path_in_candidate_environment: Literal[False] = False
    process_isolation_only: Literal[True] = True
    human_evidence: Literal[False] = False
    started_at: datetime
    completed_at: datetime
    status: Literal["CONFORMANCE_PASS"] = "CONFORMANCE_PASS"
    authority_signature: DetachedAuthoritySignature

    @model_validator(mode="after")
    def receipt_is_complete(self) -> "ExternalSignerConformanceReceipt":
        expected = {
            "PREPARE_ACTIVATION",
            "MINT_LIVE",
            "CAPTURE_AMAP_EFFECT",
            "CAPTURE_QWEN_EFFECT",
            "SIGN_AUTOMATED_COMPONENT",
            "SIGN_LIVE_COMPONENT",
            "SIGN_PANEL_COMPONENT",
            "SIGN_SEALED_COMPONENT",
            "SIGN_FINAL_GATE",
        }
        if set(self.supported_operations) != expected:
            raise ValueError("external signer operation surface is incomplete")
        if len(self.supported_operations) != len(set(self.supported_operations)):
            raise ValueError("external signer operations must be unique")
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("external signer timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("external signer completion precedes its start")
        if self.authority_signature.authority_role != "SEALED_CUSTODY":
            raise ValueError("external signer conformance requires custody signature")
        return self


def verify_external_signer_conformance_receipt(
    *,
    receipt_path: Path,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    expected_authority_policy_sha256: str,
    expected_signer_bundle_sha256: str,
) -> tuple[ExternalSignerConformanceReceipt, str]:
    """Verify one externally produced conformance receipt without trusting its path."""

    snapshot = read_external_snapshot(receipt_path, repository_root)
    try:
        receipt = ExternalSignerConformanceReceipt.model_validate_json(snapshot.content)
    except ValueError as exc:
        raise ExternalAuthorityError("external signer conformance receipt is invalid") from exc
    if (
        receipt.candidate_commit != expected_candidate_commit
        or receipt.candidate_tree != expected_candidate_tree
        or receipt.authority_policy_sha256 != expected_authority_policy_sha256
        or receipt.signer_bundle_sha256 != expected_signer_bundle_sha256
    ):
        raise ExternalAuthorityError("external signer conformance binding mismatch")
    verify_candidate_protocol_schema_bindings(
        repository_root=repository_root,
        candidate_commit=expected_candidate_commit,
    )
    try:
        verify_payload_signature(
            payload=unsigned_payload(receipt),
            signature=receipt.authority_signature,
            manifest=manifest,
            expected_role="SEALED_CUSTODY",
        )
    except ValueError as exc:
        raise ExternalAuthorityError("external signer conformance signature is invalid") from exc
    return receipt, snapshot.sha256
