from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.agent_gate_v1.authority import (
    AuthorityPolicyError,
    _validate_contract_code_bindings,
)
from evals.agent_gate_v1.contracts import AgentGateAuthorityManifest
from evals.agent_gate_v1.development_signing import (
    generate_authority_keypair,
    sign_payload_for_development,
)
from evals.agent_gate_v1 import external_authority as external_authority_module
from evals.agent_gate_v1.external_authority import (
    ExternalAuthorityError,
    ExternalSignerConformanceExpectedBindings,
    ExternalSignerConformanceExpectedBindingsReceipt,
    ExternalSignerConformanceReceipt,
    GovernedCandidateAuthorityContext,
    MAX_EXTERNAL_CONFORMANCE_BYTES,
    _load_governed_candidate_authority_context,
    verify_candidate_protocol_schema_bindings,
    verify_external_signer_conformance_receipt,
)
from evals.agent_gate_v1.final_gate import (
    AgentGatePassError,
    _verify_runtime_provenance,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
GENERAL_ROOT = BACKEND_ROOT / "eval_data" / "agent_gate_v1"
OPERATIONS = [
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


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _commit_minimal_repository(repository: Path) -> str:
    _git(repository, "init")
    _git(repository, "config", "user.email", "agent-gate-tests@example.invalid")
    _git(repository, "config", "user.name", "Agent Gate Tests")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "test fixture")
    return _git(repository, "rev-parse", "HEAD")


def _write_minimal_protocol_schema_contract(repository: Path) -> None:
    data_root = repository / "backend" / "eval_data" / "agent_gate_v1"
    data_root.mkdir(parents=True, exist_ok=True)
    schema_names = (
        "external_signer_conformance_receipt.schema.json",
        "external_signer_conformance_expected_bindings.schema.json",
    )
    bindings = {}
    for schema_name in schema_names:
        schema_bytes = (GENERAL_ROOT / schema_name).read_bytes()
        (data_root / schema_name).write_bytes(schema_bytes)
        bindings[schema_name] = hashlib.sha256(schema_bytes).hexdigest()
    (data_root / "protocol_contract.json").write_text(
        json.dumps({"schema_sha256": bindings}),
        encoding="utf-8",
    )


def _manifest_with_test_authority_keys(
    tmp_path: Path,
) -> tuple[AgentGateAuthorityManifest, dict[str, Path]]:
    manifest = AgentGateAuthorityManifest.model_validate_json(
        (GENERAL_ROOT / "authority_policy.json").read_bytes()
    )
    keys: dict[str, Path] = {}
    replacements = {}
    for role, authority_id in (
        ("SEALED_CUSTODY", "AUTH-TEST-CUSTODY-000001"),
        ("FINAL_GATE", "AUTH-TEST-FINAL-GATE-000001"),
    ):
        private_key_path = tmp_path / f"{role.lower()}-private.key"
        replacements[role] = generate_authority_keypair(
            private_key_path=private_key_path,
            repository_root=REPOSITORY_ROOT,
            role=role,
            authority_id=authority_id,
            task_id=f"external-signer-conformance-{role.lower()}-test",
        )
        keys[role] = private_key_path
    authorities = [
        replacements.get(authority.role, authority)
        for authority in manifest.authorities
    ]
    return manifest.model_copy(update={"authorities": authorities}), keys


def _signed_receipt(
    tmp_path: Path,
    *,
    candidate_commit: str,
    candidate_tree: str,
) -> tuple[
    ExternalSignerConformanceReceipt,
    ExternalSignerConformanceExpectedBindingsReceipt,
    AgentGateAuthorityManifest,
]:
    manifest, private_key_paths = _manifest_with_test_authority_keys(tmp_path)
    manifest = manifest.model_copy(
        update={
            "custody_registry_identity_sha256": "3" * 64,
            "custody_registry_path_sha256": "4" * 64,
        }
    )
    unsigned = {
        "schema_version": "external-signer-conformance-receipt-v1",
        "broker_id": "BROKER-EXTERNAL-0001",
        "signer_bundle_sha256": "1" * 64,
        "supervisor_bundle_sha256": "2" * 64,
        "broker_registry_identity_sha256": "3" * 64,
        "broker_registry_path_sha256": "4" * 64,
        "operation_role_bindings_sha256": "5" * 64,
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "authority_policy_sha256": "8" * 64,
        "conformance_run_id": "SIGCONF-0123456789ABCDEF0123456789ABCDEF",
        "request_sha256": "9" * 64,
        "challenge_sha256": "a" * 64,
        "commit_sha256": "b" * 64,
        "response_sha256": "c" * 64,
        "supported_operations": OPERATIONS,
        "two_stage": True,
        "exact_retry_idempotent": True,
        "conflicting_request_rejected": True,
        "expired_request_rejected": True,
        "replay_rejected": True,
        "tamper_rejected": True,
        "caller_payload_accepted": False,
        "caller_role_accepted": False,
        "caller_verdict_accepted": False,
        "generic_signing_endpoint": False,
        "purpose_specific_payload_construction": True,
        "authority_supervisor_separate": True,
        "role_process_count": 8,
        "role_private_key_count": 8,
        "shared_private_key_process": False,
        "sanitized_environment_keys_sha256": "d" * 64,
        "inherited_handle_manifest_sha256": "e" * 64,
        "child_process_manifest_sha256": "f" * 64,
        "observed_exit_code": 0,
        "timed_out": False,
        "signer_network_access": False,
        "repository_external": True,
        "imports_candidate_code": False,
        "private_key_in_candidate_process": False,
        "key_path_in_candidate_environment": False,
        "process_isolation_only": True,
        "human_evidence": False,
        "started_at": "2026-08-28T10:00:00Z",
        "completed_at": "2026-08-28T10:01:00Z",
        "status": "CONFORMANCE_PASS",
    }
    signature = sign_payload_for_development(
        payload=unsigned,
        private_key_path=private_key_paths["SEALED_CUSTODY"],
        repository_root=REPOSITORY_ROOT,
        authority=next(
            authority
            for authority in manifest.authorities
            if authority.role == "SEALED_CUSTODY"
        ),
    )
    receipt = ExternalSignerConformanceReceipt.model_validate_json(
        json.dumps({**unsigned, "authority_signature": signature.model_dump(mode="json")}),
        strict=True,
    )
    bindings = _expected_bindings(receipt)
    expected_unsigned = {
        "schema_version": "external-signer-conformance-expected-bindings-v1",
        "bindings": bindings.model_dump(mode="json"),
        "observed_at": "2026-08-28T10:01:30Z",
        "process_isolation_only": True,
        "human_evidence": False,
        "status": "EXPECTED_BINDINGS_FROZEN",
    }
    expected_signature = sign_payload_for_development(
        payload=expected_unsigned,
        private_key_path=private_key_paths["FINAL_GATE"],
        repository_root=REPOSITORY_ROOT,
        authority=next(
            authority
            for authority in manifest.authorities
            if authority.role == "FINAL_GATE"
        ),
    )
    expected_receipt = ExternalSignerConformanceExpectedBindingsReceipt.model_validate_json(
        json.dumps(
            {
                **expected_unsigned,
                "authority_signature": expected_signature.model_dump(mode="json"),
            }
        ),
        strict=True,
    )
    return receipt, expected_receipt, manifest


def _expected_bindings(
    receipt: ExternalSignerConformanceReceipt,
) -> ExternalSignerConformanceExpectedBindings:
    return ExternalSignerConformanceExpectedBindings.model_validate(
        {
            field_name: getattr(receipt, field_name)
            for field_name in ExternalSignerConformanceExpectedBindings.model_fields
        }
    )


def _governed_context(
    *,
    manifest: AgentGateAuthorityManifest,
    candidate_commit: str,
    candidate_tree: str,
) -> GovernedCandidateAuthorityContext:
    return GovernedCandidateAuthorityContext(
        manifest=manifest,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        authority_policy_sha256="8" * 64,
    )


def _write_conformance_pair(
    tmp_path: Path,
    *,
    receipt: ExternalSignerConformanceReceipt,
    expected: ExternalSignerConformanceExpectedBindingsReceipt,
) -> tuple[Path, Path]:
    receipt_path = tmp_path / "external-signer-conformance.json"
    expected_path = tmp_path / "external-signer-expected-bindings.json"
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")
    expected_path.write_text(expected.model_dump_json(), encoding="utf-8")
    return receipt_path, expected_path


def test_governed_candidate_context_comes_from_canonical_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_commit = _git(REPOSITORY_ROOT, "rev-parse", "HEAD")
    monkeypatch.setattr(
        external_authority_module,
        "_canonical_remote_ref",
        lambda *_args: candidate_commit,
    )
    context = _load_governed_candidate_authority_context(
        repository_root=REPOSITORY_ROOT,
        candidate_commit=candidate_commit,
    )

    assert context.candidate_commit == candidate_commit
    assert context.candidate_tree == _git(
        REPOSITORY_ROOT,
        "show",
        "-s",
        "--format=%T",
        candidate_commit,
    )
    assert context.authority_policy_sha256 == hashlib.sha256(
        (GENERAL_ROOT / "authority_policy.json").read_bytes()
    ).hexdigest()
    assert context.manifest.authority_phase == "BOOTSTRAP"

    monkeypatch.setattr(
        external_authority_module,
        "_canonical_remote_ref",
        lambda *_args: "0" * 40,
    )
    with pytest.raises(ExternalAuthorityError, match="canonical remote readback"):
        _load_governed_candidate_authority_context(
            repository_root=REPOSITORY_ROOT,
            candidate_commit=candidate_commit,
        )


def test_canonical_remote_ref_requires_one_exact_branch_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = "refs/heads/codex/trip-check-product-reset"
    candidate_commit = "1" * 40
    monkeypatch.setattr(
        external_authority_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{candidate_commit}\t{reference}\n",
        ),
    )
    assert (
        external_authority_module._canonical_remote_ref(REPOSITORY_ROOT, reference)
        == candidate_commit
    )

    monkeypatch.setattr(
        external_authority_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                f"{candidate_commit}\t{reference}\n"
                f"{'2' * 40}\t{reference}\n"
            ),
        ),
    )
    with pytest.raises(ExternalAuthorityError, match="ambiguous or absent"):
        external_authority_module._canonical_remote_ref(REPOSITORY_ROOT, reference)


def test_external_signer_conformance_receipt_verifies_exact_external_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "candidate-repository"
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)
    candidate_tree = _git(repository, "show", "-s", "--format=%T", "HEAD")
    receipt, expected, manifest = _signed_receipt(
        tmp_path,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    receipt_path, expected_path = _write_conformance_pair(
        tmp_path,
        receipt=receipt,
        expected=expected,
    )
    monkeypatch.setattr(
        external_authority_module,
        "_load_governed_candidate_authority_context",
        lambda **_kwargs: _governed_context(
            manifest=manifest,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
        ),
    )

    verified, receipt_sha256, expected_sha256 = (
        verify_external_signer_conformance_receipt(
        receipt_path=receipt_path,
        expected_bindings_path=expected_path,
        repository_root=repository,
        candidate_commit=candidate_commit,
        )
    )

    assert verified == receipt
    assert receipt_sha256 == hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    assert expected_sha256 == hashlib.sha256(expected_path.read_bytes()).hexdigest()


def test_external_signer_expected_bindings_must_match_governed_git_and_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "governed-binding-repository"
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)
    candidate_tree = _git(repository, "show", "-s", "--format=%T", "HEAD")
    receipt, expected, manifest = _signed_receipt(
        tmp_path,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    receipt_path, expected_path = _write_conformance_pair(
        tmp_path,
        receipt=receipt,
        expected=expected,
    )
    base = _governed_context(
        manifest=manifest,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    replacements = {
        "candidate_commit": base.__class__(
            manifest=manifest,
            candidate_commit="0" * 40,
            candidate_tree=candidate_tree,
            authority_policy_sha256="8" * 64,
        ),
        "candidate_tree": base.__class__(
            manifest=manifest,
            candidate_commit=candidate_commit,
            candidate_tree="0" * 40,
            authority_policy_sha256="8" * 64,
        ),
        "authority_policy_sha256": base.__class__(
            manifest=manifest,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
            authority_policy_sha256="0" * 64,
        ),
        "broker_registry_identity_sha256": base.__class__(
            manifest=manifest.model_copy(
                update={"custody_registry_identity_sha256": "0" * 64}
            ),
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
            authority_policy_sha256="8" * 64,
        ),
        "broker_registry_path_sha256": base.__class__(
            manifest=manifest.model_copy(
                update={"custody_registry_path_sha256": "0" * 64}
            ),
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
            authority_policy_sha256="8" * 64,
        ),
    }
    for field_name, context in replacements.items():
        monkeypatch.setattr(
            external_authority_module,
            "_load_governed_candidate_authority_context",
            lambda **_kwargs: context,
        )
        with pytest.raises(ExternalAuthorityError, match=field_name):
            verify_external_signer_conformance_receipt(
                receipt_path=receipt_path,
                expected_bindings_path=expected_path,
                repository_root=repository,
                candidate_commit=candidate_commit,
            )


def test_external_signer_conformance_rejects_binding_and_signature_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "candidate-repository"
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)
    candidate_tree = _git(repository, "show", "-s", "--format=%T", "HEAD")
    receipt, expected_receipt, manifest = _signed_receipt(
        tmp_path,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    receipt_path, expected_path = _write_conformance_pair(
        tmp_path,
        receipt=receipt,
        expected=expected_receipt,
    )
    monkeypatch.setattr(
        external_authority_module,
        "_load_governed_candidate_authority_context",
        lambda **_kwargs: _governed_context(
            manifest=manifest,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
        ),
    )

    replacements = {
        "broker_id": "BROKER-EXTERNAL-9999",
        "signer_bundle_sha256": "0" * 64,
        "supervisor_bundle_sha256": "0" * 64,
        "broker_registry_identity_sha256": "0" * 64,
        "broker_registry_path_sha256": "0" * 64,
        "operation_role_bindings_sha256": "0" * 64,
        "candidate_commit": "0" * 40,
        "candidate_tree": "0" * 40,
        "authority_policy_sha256": "0" * 64,
        "conformance_run_id": "SIGCONF-FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
        "request_sha256": "0" * 64,
        "challenge_sha256": "0" * 64,
        "commit_sha256": "0" * 64,
        "response_sha256": "0" * 64,
    }
    for field_name, replacement in replacements.items():
        changed_bindings = expected_receipt.bindings.model_copy(
            update={field_name: replacement}
        )
        changed_expected = expected_receipt.model_copy(update={"bindings": changed_bindings})
        expected_path.write_text(changed_expected.model_dump_json(), encoding="utf-8")
        with pytest.raises(ExternalAuthorityError, match="signature is invalid"):
            verify_external_signer_conformance_receipt(
                receipt_path=receipt_path,
                expected_bindings_path=expected_path,
                repository_root=repository,
                candidate_commit=candidate_commit,
            )
    expected_path.write_text(expected_receipt.model_dump_json(), encoding="utf-8")

    tampered = receipt.model_dump(mode="json")
    tampered["completed_at"] = "2026-08-28T10:02:00Z"
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExternalAuthorityError, match="signature is invalid"):
        verify_external_signer_conformance_receipt(
            receipt_path=receipt_path,
            expected_bindings_path=expected_path,
            repository_root=repository,
            candidate_commit=candidate_commit,
        )


def test_external_signer_schema_rejects_incomplete_or_duplicate_operations() -> None:
    schema = json.loads(
        (GENERAL_ROOT / "external_signer_conformance_receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert not {
        "payload",
        "role",
        "verdict",
        "aggregate",
        "expected_sha256",
    }.intersection(schema["properties"])
    assert schema["properties"]["generic_signing_endpoint"]["const"] is False
    assert schema["properties"]["caller_payload_accepted"]["const"] is False
    assert set(schema["required"]) == set(schema["properties"])
    with pytest.raises(ValueError, match="operation surface is incomplete"):
        ExternalSignerConformanceReceipt.model_validate(
            {
                **_minimal_receipt_payload(),
                "supported_operations": [*OPERATIONS[:-1], OPERATIONS[0]],
            }
        )


def _minimal_receipt_payload() -> dict[str, object]:
    return {
        "schema_version": "external-signer-conformance-receipt-v1",
        "broker_id": "BROKER-EXTERNAL-0001",
        "signer_bundle_sha256": "1" * 64,
        "supervisor_bundle_sha256": "2" * 64,
        "broker_registry_identity_sha256": "3" * 64,
        "broker_registry_path_sha256": "4" * 64,
        "operation_role_bindings_sha256": "5" * 64,
        "candidate_commit": "6" * 40,
        "candidate_tree": "7" * 40,
        "authority_policy_sha256": "8" * 64,
        "conformance_run_id": "SIGCONF-0123456789ABCDEF0123456789ABCDEF",
        "request_sha256": "9" * 64,
        "challenge_sha256": "a" * 64,
        "commit_sha256": "b" * 64,
        "response_sha256": "c" * 64,
        "supported_operations": OPERATIONS,
        "two_stage": True,
        "exact_retry_idempotent": True,
        "conflicting_request_rejected": True,
        "expired_request_rejected": True,
        "replay_rejected": True,
        "tamper_rejected": True,
        "caller_payload_accepted": False,
        "caller_role_accepted": False,
        "caller_verdict_accepted": False,
        "generic_signing_endpoint": False,
        "purpose_specific_payload_construction": True,
        "authority_supervisor_separate": True,
        "role_process_count": 8,
        "role_private_key_count": 8,
        "shared_private_key_process": False,
        "sanitized_environment_keys_sha256": "d" * 64,
        "inherited_handle_manifest_sha256": "e" * 64,
        "child_process_manifest_sha256": "f" * 64,
        "observed_exit_code": 0,
        "timed_out": False,
        "signer_network_access": False,
        "repository_external": True,
        "imports_candidate_code": False,
        "private_key_in_candidate_process": False,
        "key_path_in_candidate_environment": False,
        "process_isolation_only": True,
        "human_evidence": False,
        "started_at": datetime(2026, 8, 28, 10, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 8, 28, 10, 1, tzinfo=UTC),
        "status": "CONFORMANCE_PASS",
        "authority_signature": {
            "authority_role": "SEALED_CUSTODY",
            "authority_id": "AUTH-TEST-CUSTODY-000001",
            "algorithm": "ED25519",
            "signed_payload_sha256": "0" * 64,
            "signature_base64": "A" * 88,
        },
    }


def test_external_signer_conformance_requires_every_security_claim() -> None:
    payload = _minimal_receipt_payload()
    required_claims = {
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
        "role_process_count",
        "role_private_key_count",
        "shared_private_key_process",
        "observed_exit_code",
        "timed_out",
        "signer_network_access",
        "repository_external",
        "imports_candidate_code",
        "private_key_in_candidate_process",
        "key_path_in_candidate_environment",
        "process_isolation_only",
        "human_evidence",
        "status",
    }
    for field_name in required_claims:
        missing = dict(payload)
        missing.pop(field_name)
        with pytest.raises(ValueError):
            ExternalSignerConformanceReceipt.model_validate(missing)


def test_external_signer_wire_rejects_schema_invalid_normalization_and_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "strict-wire-repository"
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)
    candidate_tree = _git(repository, "show", "-s", "--format=%T", "HEAD")
    receipt, expected, manifest = _signed_receipt(
        tmp_path,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    receipt_path, expected_path = _write_conformance_pair(
        tmp_path,
        receipt=receipt,
        expected=expected,
    )
    monkeypatch.setattr(
        external_authority_module,
        "_load_governed_candidate_authority_context",
        lambda **_kwargs: _governed_context(
            manifest=manifest,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
        ),
    )

    payload = receipt.model_dump(mode="json")
    variants = []
    for field_name, value in (
        ("two_stage", 1),
        ("human_evidence", 0),
        ("observed_exit_code", False),
        ("role_process_count", 8.0),
        ("started_at", 1_777_374_000),
    ):
        changed = dict(payload)
        changed[field_name] = value
        variants.append(json.dumps(changed))
    missing_algorithm = json.loads(receipt.model_dump_json())
    missing_algorithm["authority_signature"].pop("algorithm")
    variants.append(json.dumps(missing_algorithm))
    variants.append(
        receipt.model_dump_json().replace(
            '"status":"CONFORMANCE_PASS"',
            '"status":"CONFORMANCE_FAIL","status":"CONFORMANCE_PASS"',
        )
    )

    for raw in variants:
        receipt_path.write_text(raw, encoding="utf-8")
        with pytest.raises(ExternalAuthorityError, match="invalid|duplicate keys"):
            verify_external_signer_conformance_receipt(
                receipt_path=receipt_path,
                expected_bindings_path=expected_path,
                repository_root=repository,
                candidate_commit=candidate_commit,
            )


def test_external_signer_expected_bindings_are_strict_distinct_and_size_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "strict-expected-repository"
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)
    candidate_tree = _git(repository, "show", "-s", "--format=%T", "HEAD")
    receipt, expected, manifest = _signed_receipt(
        tmp_path,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    receipt_path, expected_path = _write_conformance_pair(
        tmp_path,
        receipt=receipt,
        expected=expected,
    )
    monkeypatch.setattr(
        external_authority_module,
        "_load_governed_candidate_authority_context",
        lambda **_kwargs: _governed_context(
            manifest=manifest,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
        ),
    )

    invalid_expected = expected.model_dump(mode="json")
    invalid_expected["process_isolation_only"] = 1
    expected_path.write_text(json.dumps(invalid_expected), encoding="utf-8")
    with pytest.raises(ExternalAuthorityError, match="expected bindings are invalid"):
        verify_external_signer_conformance_receipt(
            receipt_path=receipt_path,
            expected_bindings_path=expected_path,
            repository_root=repository,
            candidate_commit=candidate_commit,
        )

    expected_path.write_text(expected.model_dump_json(), encoding="utf-8")
    with pytest.raises(ExternalAuthorityError, match="distinct artifacts"):
        verify_external_signer_conformance_receipt(
            receipt_path=expected_path,
            expected_bindings_path=expected_path,
            repository_root=repository,
            candidate_commit=candidate_commit,
        )

    oversized = receipt.model_dump_json() + (
        " " * (MAX_EXTERNAL_CONFORMANCE_BYTES + 1)
    )
    receipt_path.write_text(oversized, encoding="utf-8")
    with pytest.raises(ExternalAuthorityError, match="exceeds size limit"):
        verify_external_signer_conformance_receipt(
            receipt_path=receipt_path,
            expected_bindings_path=expected_path,
            repository_root=repository,
            candidate_commit=candidate_commit,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle link-race contract")
def test_limited_external_snapshot_rejects_hardlink_added_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "external-receipt.json"
    alias = tmp_path / "external-receipt-alias.json"
    source.write_text("{}", encoding="utf-8")
    real_fstat = external_authority_module.os.fstat
    linked = False

    def link_after_first_fstat(descriptor: int):
        nonlocal linked
        observed = real_fstat(descriptor)
        if not linked:
            os.link(source, alias)
            linked = True
        return observed

    monkeypatch.setattr(
        external_authority_module.os,
        "fstat",
        link_after_first_fstat,
    )
    with pytest.raises(ExternalAuthorityError, match="path is invalid"):
        external_authority_module._read_limited_external_snapshot(
            path=source,
            repository_root=REPOSITORY_ROOT,
            artifact_label="external signer test receipt",
        )
    assert alias.exists()
    assert source.stat().st_nlink == 2


def test_external_signer_schemas_require_nested_signature_and_all_top_level_fields() -> None:
    for filename in (
        "external_signer_conformance_receipt.schema.json",
        "external_signer_conformance_expected_bindings.schema.json",
    ):
        schema = json.loads((GENERAL_ROOT / filename).read_text(encoding="utf-8"))
        assert set(schema["required"]) == set(schema["properties"])
        signature = schema["$defs"]["RequiredDetachedAuthoritySignature"]
        assert set(signature["required"]) == set(signature["properties"])
        assert all("default" not in value for value in signature["properties"].values())


def test_protocol_contract_transitively_binds_external_authority_code() -> None:
    protocol = json.loads((GENERAL_ROOT / "protocol_contract.json").read_text())
    module = BACKEND_ROOT / "evals" / "agent_gate_v1" / "external_authority.py"
    schema = GENERAL_ROOT / "external_signer_conformance_receipt.schema.json"
    assert protocol["contract_code_sha256"]["external_authority.py"] == (
        hashlib.sha256(module.read_bytes()).hexdigest()
    )
    assert protocol["schema_sha256"][schema.name] == hashlib.sha256(
        schema.read_bytes()
    ).hexdigest()
    assert not {
        "automation_runner_browser_package-lock.json",
        "automation_runner_browser_package.json",
        "trip_text_cards_v1_contracts.py",
        "trip_text_cards_v1_scorer.py",
    }.intersection(protocol["contract_code_sha256"])
    assert all(
        (BACKEND_ROOT / "evals" / "agent_gate_v1" / filename).is_file()
        for filename in protocol["contract_code_sha256"]
    )


def test_complete_generated_contract_code_bindings_validate_from_committed_snapshot(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "complete-contract-repository"
    contracts = (
        (
            GENERAL_ROOT / "protocol_contract.json",
            repository / "backend" / "eval_data" / "agent_gate_v1",
            BACKEND_ROOT / "evals" / "agent_gate_v1",
            repository / "backend" / "evals" / "agent_gate_v1",
        ),
        (
            BACKEND_ROOT
            / "eval_data"
            / "trip_text_cards_agent_v2"
            / "agent_evaluation_contract.json",
            repository / "backend" / "eval_data" / "trip_text_cards_agent_v2",
            BACKEND_ROOT / "evals" / "trip_text_cards_agent_v2",
            repository / "backend" / "evals" / "trip_text_cards_agent_v2",
        ),
    )
    for contract_source, contract_target_root, code_source_root, code_target_root in contracts:
        contract = json.loads(contract_source.read_text(encoding="utf-8"))
        contract_target_root.mkdir(parents=True, exist_ok=True)
        code_target_root.mkdir(parents=True, exist_ok=True)
        (contract_target_root / contract_source.name).write_bytes(
            contract_source.read_bytes()
        )
        for filename in contract["contract_code_sha256"]:
            (code_target_root / filename).write_bytes(
                (code_source_root / filename).read_bytes()
            )
    candidate_commit = _commit_minimal_repository(repository)

    _validate_contract_code_bindings(repository, candidate_commit)


def test_candidate_protocol_schema_binding_rejects_committed_schema_drift(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "schema-repository"
    _write_minimal_protocol_schema_contract(repository)
    bound_commit = _commit_minimal_repository(repository)
    verify_candidate_protocol_schema_bindings(
        repository_root=repository,
        candidate_commit=bound_commit,
    )

    schema_path = (
        repository
        / "backend"
        / "eval_data"
        / "agent_gate_v1"
        / "external_signer_conformance_receipt.schema.json"
    )
    schema_path.write_bytes(schema_path.read_bytes() + b"\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "tamper committed schema")
    tampered_commit = _git(repository, "rev-parse", "HEAD")
    with pytest.raises(ExternalAuthorityError, match="schema hash mismatch"):
        verify_candidate_protocol_schema_bindings(
            repository_root=repository,
            candidate_commit=tampered_commit,
        )


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("duplicate", "duplicate keys"),
        ("empty", "no schema bindings"),
        ("missing_required", "schema binding is absent"),
        ("path_traversal", "schema binding is invalid"),
        ("invalid_digest", "schema binding is invalid"),
        ("too_many", "too many schema bindings"),
    ],
)
def test_candidate_protocol_schema_binding_rejects_malformed_contract(
    tmp_path: Path,
    variant: str,
    message: str,
) -> None:
    repository = tmp_path / f"malformed-{variant}"
    data_root = repository / "backend" / "eval_data" / "agent_gate_v1"
    data_root.mkdir(parents=True)
    schema_name = "external_signer_conformance_receipt.schema.json"
    schema_bytes = (GENERAL_ROOT / schema_name).read_bytes()
    (data_root / schema_name).write_bytes(schema_bytes)
    schema_digest = hashlib.sha256(schema_bytes).hexdigest()
    if variant == "duplicate":
        contract_bytes = (
            '{"schema_sha256":{"'
            + schema_name
            + '":"'
            + schema_digest
            + '","'
            + schema_name
            + '":"'
            + schema_digest
            + '"}}'
        ).encode("utf-8")
    else:
        if variant == "empty":
            bindings: dict[str, str] = {}
        elif variant == "missing_required":
            bindings = {"other.schema.json": "0" * 64}
        elif variant == "path_traversal":
            bindings = {
                "../escape.schema.json": "0" * 64,
                schema_name: schema_digest,
            }
        elif variant == "invalid_digest":
            bindings = {schema_name: "0" * 63}
        else:
            bindings = {schema_name: schema_digest}
            bindings.update(
                {
                    f"schema_{index:03d}.schema.json": "0" * 64
                    for index in range(128)
                }
            )
        contract_bytes = json.dumps({"schema_sha256": bindings}).encode("utf-8")
    (data_root / "protocol_contract.json").write_bytes(contract_bytes)
    candidate_commit = _commit_minimal_repository(repository)

    with pytest.raises(ExternalAuthorityError, match=message):
        verify_candidate_protocol_schema_bindings(
            repository_root=repository,
            candidate_commit=candidate_commit,
        )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("subject", ["contract", "schema"])
def test_candidate_protocol_schema_binding_rejects_non_standard_json_constants(
    tmp_path: Path,
    constant: str,
    subject: str,
) -> None:
    repository = tmp_path / f"non-finite-{subject}-{constant.replace('-', 'minus')}"
    data_root = repository / "backend" / "eval_data" / "agent_gate_v1"
    data_root.mkdir(parents=True)
    schema_name = "external_signer_conformance_receipt.schema.json"
    schema_bytes = (
        '{"type":"object","properties":{},"non_finite":'
        + (constant if subject == "schema" else "null")
        + "}"
    ).encode("utf-8")
    (data_root / schema_name).write_bytes(schema_bytes)
    contract_bytes = (
        '{"schema_sha256":{"'
        + schema_name
        + '":"'
        + hashlib.sha256(schema_bytes).hexdigest()
        + '"},"non_finite":'
        + (constant if subject == "contract" else "null")
        + "}"
    ).encode("utf-8")
    (data_root / "protocol_contract.json").write_bytes(contract_bytes)
    candidate_commit = _commit_minimal_repository(repository)

    with pytest.raises(ExternalAuthorityError, match="non-standard constant"):
        verify_candidate_protocol_schema_bindings(
            repository_root=repository,
            candidate_commit=candidate_commit,
        )


@pytest.mark.parametrize(
    "number",
    [
        "1e999",
        "-1e999",
        "1e-999",
        "-1e-999",
        "9007199254740992",
        "-9007199254740992",
    ],
)
@pytest.mark.parametrize("subject", ["contract", "schema"])
def test_candidate_protocol_schema_binding_rejects_non_interoperable_numbers(
    tmp_path: Path,
    number: str,
    subject: str,
) -> None:
    repository = tmp_path / f"number-{subject}-{hashlib.sha256(number.encode()).hexdigest()[:8]}"
    data_root = repository / "backend" / "eval_data" / "agent_gate_v1"
    data_root.mkdir(parents=True)
    schema_name = "external_signer_conformance_receipt.schema.json"
    schema_bytes = (
        '{"type":"object","properties":{},"number":'
        + (number if subject == "schema" else "0")
        + "}"
    ).encode("utf-8")
    (data_root / schema_name).write_bytes(schema_bytes)
    contract_bytes = (
        '{"schema_sha256":{"'
        + schema_name
        + '":"'
        + hashlib.sha256(schema_bytes).hexdigest()
        + '"},"number":'
        + (number if subject == "contract" else "0")
        + "}"
    ).encode("utf-8")
    (data_root / "protocol_contract.json").write_bytes(contract_bytes)
    candidate_commit = _commit_minimal_repository(repository)

    with pytest.raises(ExternalAuthorityError, match="interoperable range"):
        verify_candidate_protocol_schema_bindings(
            repository_root=repository,
            candidate_commit=candidate_commit,
        )


@pytest.mark.parametrize(
    "number",
    ["1e308", "-1e308", "5e-324", "9007199254740991", "-9007199254740991"],
)
def test_candidate_protocol_schema_binding_accepts_interoperable_numbers(
    tmp_path: Path,
    number: str,
) -> None:
    repository = tmp_path / f"safe-number-{hashlib.sha256(number.encode()).hexdigest()[:8]}"
    data_root = repository / "backend" / "eval_data" / "agent_gate_v1"
    data_root.mkdir(parents=True)
    schema_name = "external_signer_conformance_receipt.schema.json"
    schema_bytes = (
        '{"type":"object","properties":{},"number":' + number + "}"
    ).encode("utf-8")
    (data_root / schema_name).write_bytes(schema_bytes)
    (data_root / "protocol_contract.json").write_text(
        json.dumps(
            {
                "schema_sha256": {
                    schema_name: hashlib.sha256(schema_bytes).hexdigest()
                }
            }
        ),
        encoding="utf-8",
    )
    candidate_commit = _commit_minimal_repository(repository)

    verify_candidate_protocol_schema_bindings(
        repository_root=repository,
        candidate_commit=candidate_commit,
    )


def test_contract_code_binding_rejects_unrecorded_external_authority_change(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "contract-repository"
    gate_source = repository / "backend" / "evals" / "agent_gate_v1"
    gate_data = repository / "backend" / "eval_data" / "agent_gate_v1"
    annotation_source = repository / "backend" / "evals" / "trip_text_cards_agent_v2"
    annotation_data = (
        repository / "backend" / "eval_data" / "trip_text_cards_agent_v2"
    )
    for directory in (gate_source, gate_data, annotation_source, annotation_data):
        directory.mkdir(parents=True, exist_ok=True)
    external_code = b"BOUND_EXTERNAL_AUTHORITY = True\n"
    probe_code = b"BOUND_AGENT_EVALUATION = True\n"
    (gate_source / "external_authority.py").write_bytes(external_code)
    (annotation_source / "probe.py").write_bytes(probe_code)
    (gate_data / "protocol_contract.json").write_text(
        json.dumps(
            {
                "contract_code_sha256": {
                    "external_authority.py": hashlib.sha256(external_code).hexdigest()
                }
            }
        ),
        encoding="utf-8",
    )
    (annotation_data / "agent_evaluation_contract.json").write_text(
        json.dumps(
            {
                "contract_code_sha256": {
                    "probe.py": hashlib.sha256(probe_code).hexdigest()
                }
            }
        ),
        encoding="utf-8",
    )
    first_commit = _commit_minimal_repository(repository)
    _validate_contract_code_bindings(repository, first_commit)

    (gate_source / "external_authority.py").write_text(
        "BOUND_EXTERNAL_AUTHORITY = False\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "tamper bound module")
    tampered_commit = _git(repository, "rev-parse", "HEAD")
    with pytest.raises(AuthorityPolicyError, match="code hash mismatch"):
        _validate_contract_code_bindings(repository, tampered_commit)


def test_final_gate_runtime_provenance_includes_external_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import final_gate

    repository = tmp_path / "runtime-repository"
    source = repository / "backend" / "evals" / "agent_gate_v1"
    source.mkdir(parents=True)
    final_path = source / "final_gate.py"
    external_path = source / "external_authority.py"
    final_path.write_text("BOUND_FINAL_GATE = True\n", encoding="utf-8")
    external_path.write_text("BOUND_EXTERNAL_AUTHORITY = True\n", encoding="utf-8")
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)

    runtime_paths = {
        "evals.agent_gate_v1.final_gate": (
            "backend/evals/agent_gate_v1/final_gate.py"
        ),
        "evals.agent_gate_v1.external_authority": (
            "backend/evals/agent_gate_v1/external_authority.py"
        ),
    }
    monkeypatch.setattr(final_gate, "RUNTIME_MODULE_PATHS", runtime_paths)
    monkeypatch.setitem(
        sys.modules,
        "evals.agent_gate_v1.final_gate",
        SimpleNamespace(__file__=str(final_path)),
    )
    monkeypatch.setitem(
        sys.modules,
        "evals.agent_gate_v1.external_authority",
        SimpleNamespace(__file__=str(external_path)),
    )
    expected = hashlib.sha256(final_path.read_bytes()).hexdigest()
    assert (
        _verify_runtime_provenance(
            repository_root=repository,
            candidate_commit=candidate_commit,
        )
        == expected
    )

    external_path.write_text("BOUND_EXTERNAL_AUTHORITY = False\n", encoding="utf-8")
    with pytest.raises(AgentGatePassError, match="not candidate-bound"):
        _verify_runtime_provenance(
            repository_root=repository,
            candidate_commit=candidate_commit,
        )


def test_candidate_repository_contains_no_generic_or_key_holding_signer() -> None:
    forbidden_template = GENERAL_ROOT / "external_signer_bundle.py"
    module = BACKEND_ROOT / "evals" / "agent_gate_v1" / "external_authority.py"
    source = module.read_text(encoding="utf-8")
    assert not forbidden_template.exists()
    assert "development_signing" not in source
    assert "generate_authority_keypair" not in source
    assert "sign_payload_for_development" not in source
    parameters = inspect.signature(
        verify_external_signer_conformance_receipt
    ).parameters
    assert not {
        "payload",
        "role",
        "verdict",
        "aggregate",
        "manifest",
        "expected",
    }.intersection(parameters)
