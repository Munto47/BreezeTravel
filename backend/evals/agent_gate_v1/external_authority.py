from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from evals.agent_gate_v1.contracts import (
    AgentGateAuthorityManifest,
    StrictModel,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1 import path_security as secure_paths
from evals.agent_gate_v1.path_security import ArtifactPathError, ArtifactSnapshot
from evals.agent_gate_v1.signing import unsigned_payload, verify_payload_signature

if sys.platform == "win32":
    import msvcrt


class ExternalAuthorityError(ValueError):
    pass


PROTOCOL_CONTRACT_PATH = (
    "backend/eval_data/agent_gate_v1/protocol_contract.json"
)
PROTOCOL_SCHEMA_ROOT = "backend/eval_data/agent_gate_v1"
REQUIRED_EXTERNAL_AUTHORITY_SCHEMAS = {
    "external_signer_conformance_expected_bindings.schema.json",
    "external_signer_conformance_receipt.schema.json"
}
BASE_EXTERNAL_AUTHORITY_SCHEMAS = {
    "external_signer_conformance_receipt.schema.json"
}
MAX_PROTOCOL_ARTIFACT_BYTES = 1_000_000
MAX_EXTERNAL_CONFORMANCE_BYTES = 256_000
MAX_PROTOCOL_SCHEMA_BINDINGS = 128
MAX_INTEROPERABLE_INTEGER = 9_007_199_254_740_991
MAX_FLOAT_TOKEN_LENGTH = 128
AUTHORITY_POLICY_PATH = "backend/eval_data/agent_gate_v1/authority_policy.json"
LEGACY_BASELINE_COMMIT = "7bdd1a6abd9c10c6076aca67f08de785027501a0"


@dataclass(frozen=True)
class GovernedCandidateAuthorityContext:
    manifest: AgentGateAuthorityManifest
    candidate_commit: str
    candidate_tree: str
    authority_policy_sha256: str


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


def _git_text(repository_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            [trusted_host_tool("git"), "-C", str(repository_root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalAuthorityError("candidate Git readback timed out") from exc
    if result.returncode != 0:
        raise ExternalAuthorityError("candidate Git readback failed")
    return result.stdout.strip()


def _canonical_remote_ref(repository_root: Path, reference: str) -> str:
    """Read one exact canonical branch head from origin, rejecting ambiguity."""

    try:
        result = subprocess.run(
            [
                trusted_host_tool("git"),
                "-C",
                str(repository_root),
                "ls-remote",
                "--refs",
                "origin",
                reference,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalAuthorityError("canonical remote readback timed out") from exc
    if result.returncode != 0:
        raise ExternalAuthorityError("canonical remote readback failed")
    rows = [line.split("\t") for line in result.stdout.splitlines() if line]
    if (
        len(rows) != 1
        or len(rows[0]) != 2
        or rows[0][1] != reference
        or re.fullmatch(r"[0-9a-f]{40}", rows[0][0]) is None
    ):
        raise ExternalAuthorityError("canonical remote reference is ambiguous or absent")
    return rows[0][0]


def _is_ancestor(repository_root: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            [
                trusted_host_tool("git"),
                "-C",
                str(repository_root),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalAuthorityError("candidate ancestry check timed out") from exc
    return result.returncode == 0


def _load_governed_candidate_authority_context(
    *,
    repository_root: Path,
    candidate_commit: str,
) -> GovernedCandidateAuthorityContext:
    """Derive candidate and authority identity from immutable Git, never a receipt."""

    root = repository_root.resolve(strict=True)
    policy_content = _git_blob(root, candidate_commit, AUTHORITY_POLICY_PATH)
    try:
        manifest = AgentGateAuthorityManifest.model_validate_json(policy_content)
    except ValueError as exc:
        raise ExternalAuthorityError("candidate authority policy is invalid") from exc
    if manifest.authority_phase != "BOOTSTRAP":
        raise ExternalAuthorityError(
            "external signer conformance must precede authority activation"
        )
    if (
        manifest.legacy_baseline_commit != LEGACY_BASELINE_COMMIT
        or not _is_ancestor(root, LEGACY_BASELINE_COMMIT, candidate_commit)
    ):
        raise ExternalAuthorityError("candidate is outside the governed baseline")
    origin = _git_text(root, "remote", "get-url", "origin")
    if origin != manifest.canonical_origin_url:
        raise ExternalAuthorityError("candidate origin is not canonical")
    canonical_tip = _git_text(root, "rev-parse", manifest.canonical_candidate_ref)
    if canonical_tip != candidate_commit:
        raise ExternalAuthorityError("candidate commit is not the canonical branch tip")
    remote_tip = _canonical_remote_ref(root, manifest.canonical_candidate_ref)
    if remote_tip != candidate_commit:
        raise ExternalAuthorityError("candidate commit has no exact canonical remote readback")
    history = _git_text(
        root,
        "log",
        "--format=%H",
        candidate_commit,
        "--",
        AUTHORITY_POLICY_PATH,
    ).splitlines()
    if len(history) != 1:
        raise ExternalAuthorityError("bootstrap authority policy history is not immutable")
    bootstrap_commit = history[0]
    if _git_blob(root, bootstrap_commit, AUTHORITY_POLICY_PATH) != policy_content:
        raise ExternalAuthorityError("bootstrap authority policy changed after creation")
    for path in manifest.bootstrap_core_paths:
        if _git_blob(root, bootstrap_commit, path) != _git_blob(
            root, candidate_commit, path
        ):
            raise ExternalAuthorityError(
                f"bootstrap authority core changed after creation: {path}"
            )
    return GovernedCandidateAuthorityContext(
        manifest=manifest,
        candidate_commit=candidate_commit,
        candidate_tree=_git_text(root, "show", "-s", "--format=%T", candidate_commit),
        authority_policy_sha256=hashlib.sha256(policy_content).hexdigest(),
    )


def _strict_json_object(
    content: bytes,
    *,
    artifact_label: str = "protocol contract",
) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ExternalAuthorityError(f"{artifact_label} has duplicate keys")
            value[key] = item
        return value

    def reject_non_finite_constant(value: str) -> None:
        raise ExternalAuthorityError(
            f"{artifact_label} contains a non-standard constant: {value}"
        )

    def parse_interoperable_integer(value: str) -> int:
        digits = value.removeprefix("-")
        if len(digits) > 16:
            raise ExternalAuthorityError(
                f"{artifact_label} integer exceeds the interoperable range"
            )
        parsed = int(value)
        if abs(parsed) > MAX_INTEROPERABLE_INTEGER:
            raise ExternalAuthorityError(
                f"{artifact_label} integer exceeds the interoperable range"
            )
        return parsed

    def parse_interoperable_float(value: str) -> float:
        if len(value) > MAX_FLOAT_TOKEN_LENGTH:
            raise ExternalAuthorityError(
                f"{artifact_label} number exceeds the interoperable range"
            )
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ExternalAuthorityError(
                f"{artifact_label} number exceeds the interoperable range"
            )
        if parsed == 0.0:
            mantissa = value.lower().split("e", maxsplit=1)[0]
            significant_digits = mantissa.lstrip("+-").replace(".", "")
            if any(digit != "0" for digit in significant_digits):
                raise ExternalAuthorityError(
                    f"{artifact_label} number underflows the interoperable range"
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
        raise ExternalAuthorityError(f"{artifact_label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ExternalAuthorityError(f"{artifact_label} must be an object")
    return value


def _read_limited_external_snapshot(
    *,
    path: Path,
    repository_root: Path,
    artifact_label: str,
) -> ArtifactSnapshot:
    """Read at most max+1 bytes from the same verified external file handle."""

    try:
        if sys.platform == "win32":
            secure_paths._reject_reparse_components(path)
            handle = secure_paths._windows_open(
                path,
                access=secure_paths._GENERIC_READ,
                share=secure_paths._FILE_SHARE_READ,
                creation=secure_paths._OPEN_EXISTING,
                flags=secure_paths._FILE_FLAG_OPEN_REPARSE_POINT,
            )
            try:
                resolved = secure_paths._windows_final_path(handle)
                secure_paths._reject_git_managed_location(resolved, repository_root)
                information = secure_paths._windows_file_information(handle)
                if information.nNumberOfLinks != 1:
                    raise ArtifactPathError("external artifact must not be a hard link")
                descriptor = msvcrt.open_osfhandle(
                    handle,
                    os.O_RDONLY | os.O_BINARY,
                )
                handle = secure_paths._INVALID_HANDLE_VALUE
                with os.fdopen(descriptor, "rb", buffering=0) as stream:
                    before = os.fstat(stream.fileno())
                    if before.st_nlink != 1:
                        raise ArtifactPathError(
                            "external artifact must remain a single-link file"
                        )
                    if before.st_size > MAX_EXTERNAL_CONFORMANCE_BYTES:
                        raise ExternalAuthorityError(
                            f"{artifact_label} exceeds size limit"
                        )
                    content = stream.read(MAX_EXTERNAL_CONFORMANCE_BYTES + 1)
                    after = os.fstat(stream.fileno())
                    final_information = secure_paths._windows_file_information(
                        msvcrt.get_osfhandle(stream.fileno())
                    )
                if len(content) > MAX_EXTERNAL_CONFORMANCE_BYTES:
                    raise ExternalAuthorityError(f"{artifact_label} exceeds size limit")
                if (
                    (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or before.st_size != len(content)
                    or after.st_size != len(content)
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_nlink != 1
                    or after.st_nlink != 1
                    or final_information.nNumberOfLinks != 1
                    or information.dwVolumeSerialNumber
                    != final_information.dwVolumeSerialNumber
                    or information.nFileIndexHigh != final_information.nFileIndexHigh
                    or information.nFileIndexLow != final_information.nFileIndexLow
                ):
                    raise ArtifactPathError(
                        "external artifact changed while it was being read"
                    )
                inode = (int(information.nFileIndexHigh) << 32) | int(
                    information.nFileIndexLow
                )
                return ArtifactSnapshot(
                    path=resolved,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    device=int(information.dwVolumeSerialNumber),
                    inode=inode,
                )
            finally:
                if handle != secure_paths._INVALID_HANDLE_VALUE:
                    secure_paths._kernel32.CloseHandle(handle)

        descriptor, resolved = secure_paths._posix_open_existing(
            path,
            repository_root,
        )
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_EXTERNAL_CONFORMANCE_BYTES:
                raise ExternalAuthorityError(f"{artifact_label} exceeds size limit")
            content = stream.read(MAX_EXTERNAL_CONFORMANCE_BYTES + 1)
            after = os.fstat(stream.fileno())
            final_path = secure_paths._posix_final_path(stream.fileno())
            secure_paths._reject_git_managed_location(final_path, repository_root)
        if len(content) > MAX_EXTERNAL_CONFORMANCE_BYTES:
            raise ExternalAuthorityError(f"{artifact_label} exceeds size limit")
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or final_path != resolved
            or before.st_size != len(content)
            or after.st_size != len(content)
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_nlink != 1
            or after.st_nlink != 1
        ):
            raise ArtifactPathError("external artifact changed while it was being read")
        return ArtifactSnapshot(
            path=resolved,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            device=before.st_dev,
            inode=before.st_ino,
        )
    except ArtifactPathError as exc:
        raise ExternalAuthorityError(f"{artifact_label} path is invalid") from exc


def verify_candidate_protocol_schema_bindings(
    *,
    repository_root: Path,
    candidate_commit: str,
) -> str:
    """Fail closed when a checked-in protocol schema drifts from its Git contract."""

    return _verify_protocol_schema_bindings(
        repository_root=repository_root,
        candidate_commit=candidate_commit,
        required_schemas=BASE_EXTERNAL_AUTHORITY_SCHEMAS,
    )


def _verify_protocol_schema_bindings(
    *,
    repository_root: Path,
    candidate_commit: str,
    required_schemas: set[str],
) -> str:

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
    if not required_schemas.issubset(bindings):
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


class RequiredDetachedAuthoritySignature(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    authority_role: Literal["SEALED_CUSTODY", "FINAL_GATE"]
    authority_id: str = Field(pattern=r"^AUTH-[A-Z0-9-]{6,100}$")
    algorithm: Literal["ED25519"]
    signed_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_base64: str = Field(min_length=80, max_length=100)


class ExternalSignerConformanceExpectedBindings(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

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


class ExternalSignerConformanceExpectedBindingsReceipt(StrictModel):
    """Separately signed authority observation; never derived from candidate output."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[
        "external-signer-conformance-expected-bindings-v1"
    ]
    bindings: ExternalSignerConformanceExpectedBindings
    observed_at: datetime
    process_isolation_only: Literal[True]
    human_evidence: Literal[False]
    status: Literal["EXPECTED_BINDINGS_FROZEN"]
    authority_signature: RequiredDetachedAuthoritySignature

    @field_validator("process_isolation_only", "human_evidence", mode="before")
    @classmethod
    def booleans_are_wire_booleans(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("expected conformance boolean has the wrong wire type")
        return value

    @model_validator(mode="after")
    def uses_distinct_final_gate_authority(
        self,
    ) -> "ExternalSignerConformanceExpectedBindingsReceipt":
        if self.authority_signature.authority_role != "FINAL_GATE":
            raise ValueError("expected conformance bindings require FINAL_GATE signature")
        if self.observed_at.tzinfo is None:
            raise ValueError("expected conformance binding timestamp must be timezone-aware")
        return self


class ExternalSignerConformanceReceipt(StrictModel):
    """Authority-owned broker proof; candidate code never constructs this payload."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["external-signer-conformance-receipt-v1"]
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
    two_stage: Literal[True]
    exact_retry_idempotent: Literal[True]
    conflicting_request_rejected: Literal[True]
    expired_request_rejected: Literal[True]
    replay_rejected: Literal[True]
    tamper_rejected: Literal[True]
    caller_payload_accepted: Literal[False]
    caller_role_accepted: Literal[False]
    caller_verdict_accepted: Literal[False]
    generic_signing_endpoint: Literal[False]
    purpose_specific_payload_construction: Literal[True]
    authority_supervisor_separate: Literal[True]
    role_process_count: Literal[8]
    role_private_key_count: Literal[8]
    shared_private_key_process: Literal[False]
    sanitized_environment_keys_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inherited_handle_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    child_process_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_exit_code: Literal[0]
    timed_out: Literal[False]
    signer_network_access: Literal[False]
    repository_external: Literal[True]
    imports_candidate_code: Literal[False]
    private_key_in_candidate_process: Literal[False]
    key_path_in_candidate_environment: Literal[False]
    process_isolation_only: Literal[True]
    human_evidence: Literal[False]
    started_at: datetime
    completed_at: datetime
    status: Literal["CONFORMANCE_PASS"]
    authority_signature: RequiredDetachedAuthoritySignature

    @field_validator(
        "two_stage",
        "exact_retry_idempotent",
        "conflicting_request_rejected",
        "expired_request_rejected",
        "replay_rejected",
        "tamper_rejected",
        "caller_payload_accepted",
        "caller_role_accepted",
        "caller_verdict_accepted",
        "generic_signing_endpoint",
        "purpose_specific_payload_construction",
        "authority_supervisor_separate",
        "shared_private_key_process",
        "timed_out",
        "signer_network_access",
        "repository_external",
        "imports_candidate_code",
        "private_key_in_candidate_process",
        "key_path_in_candidate_environment",
        "process_isolation_only",
        "human_evidence",
        mode="before",
    )
    @classmethod
    def booleans_are_wire_booleans(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("external signer boolean has the wrong wire type")
        return value

    @field_validator(
        "role_process_count",
        "role_private_key_count",
        "observed_exit_code",
        mode="before",
    )
    @classmethod
    def integers_are_wire_integers(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("external signer integer has the wrong wire type")
        return value

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
    expected_bindings_path: Path,
    repository_root: Path,
    candidate_commit: str,
) -> tuple[ExternalSignerConformanceReceipt, str, str]:
    """Verify independently signed expectations and conformance against governed Git."""

    context = _load_governed_candidate_authority_context(
        repository_root=repository_root,
        candidate_commit=candidate_commit,
    )
    expected_snapshot = _read_limited_external_snapshot(
        path=expected_bindings_path,
        repository_root=repository_root,
        artifact_label="external signer expected bindings",
    )
    _strict_json_object(
        expected_snapshot.content,
        artifact_label="external signer expected bindings",
    )
    try:
        expected_receipt = (
            ExternalSignerConformanceExpectedBindingsReceipt.model_validate_json(
                expected_snapshot.content,
                strict=True,
            )
        )
    except ValueError as exc:
        raise ExternalAuthorityError("external signer expected bindings are invalid") from exc
    try:
        verify_payload_signature(
            payload=unsigned_payload(expected_receipt),
            signature=expected_receipt.authority_signature,
            manifest=context.manifest,
            expected_role="FINAL_GATE",
        )
    except ValueError as exc:
        raise ExternalAuthorityError(
            "external signer expected bindings signature is invalid"
        ) from exc

    expected = expected_receipt.bindings
    governed_git_bindings = {
        "candidate_commit": context.candidate_commit,
        "candidate_tree": context.candidate_tree,
        "authority_policy_sha256": context.authority_policy_sha256,
        "broker_registry_identity_sha256": (
            context.manifest.custody_registry_identity_sha256
        ),
        "broker_registry_path_sha256": context.manifest.custody_registry_path_sha256,
    }
    for field_name, governed_value in governed_git_bindings.items():
        if getattr(expected, field_name) != governed_value:
            raise ExternalAuthorityError(
                f"external signer expected binding disagrees with authority: {field_name}"
            )

    snapshot = _read_limited_external_snapshot(
        path=receipt_path,
        repository_root=repository_root,
        artifact_label="external signer conformance receipt",
    )
    if (
        snapshot.path == expected_snapshot.path
        or (snapshot.device, snapshot.inode)
        == (expected_snapshot.device, expected_snapshot.inode)
    ):
        raise ExternalAuthorityError(
            "conformance receipt and expected bindings must be distinct artifacts"
        )
    _strict_json_object(
        snapshot.content,
        artifact_label="external signer conformance receipt",
    )
    try:
        receipt = ExternalSignerConformanceReceipt.model_validate_json(
            snapshot.content,
            strict=True,
        )
    except ValueError as exc:
        raise ExternalAuthorityError("external signer conformance receipt is invalid") from exc
    try:
        verify_payload_signature(
            payload=unsigned_payload(receipt),
            signature=receipt.authority_signature,
            manifest=context.manifest,
            expected_role="SEALED_CUSTODY",
        )
    except ValueError as exc:
        raise ExternalAuthorityError("external signer conformance signature is invalid") from exc
    for field_name in ExternalSignerConformanceExpectedBindings.model_fields:
        if getattr(receipt, field_name) != getattr(expected, field_name):
            raise ExternalAuthorityError(
                f"external signer conformance binding mismatch: {field_name}"
            )
    _verify_protocol_schema_bindings(
        repository_root=repository_root,
        candidate_commit=context.candidate_commit,
        required_schemas=REQUIRED_EXTERNAL_AUTHORITY_SCHEMAS,
    )
    return receipt, snapshot.sha256, expected_snapshot.sha256
