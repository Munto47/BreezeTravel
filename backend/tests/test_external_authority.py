from __future__ import annotations

import hashlib
import inspect
import json
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
from evals.agent_gate_v1.external_authority import (
    ExternalAuthorityError,
    ExternalSignerConformanceReceipt,
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
    schema_name = "external_signer_conformance_receipt.schema.json"
    schema_bytes = (GENERAL_ROOT / schema_name).read_bytes()
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


def _manifest_with_test_custody_key(
    tmp_path: Path,
) -> tuple[AgentGateAuthorityManifest, Path]:
    manifest = AgentGateAuthorityManifest.model_validate_json(
        (GENERAL_ROOT / "authority_policy.json").read_bytes()
    )
    private_key_path = tmp_path / "custody-private.key"
    custody = generate_authority_keypair(
        private_key_path=private_key_path,
        repository_root=REPOSITORY_ROOT,
        role="SEALED_CUSTODY",
        authority_id="AUTH-TEST-CUSTODY-000001",
        task_id="external-signer-conformance-test",
    )
    authorities = [
        custody if authority.role == "SEALED_CUSTODY" else authority
        for authority in manifest.authorities
    ]
    return manifest.model_copy(update={"authorities": authorities}), private_key_path


def _signed_receipt(
    tmp_path: Path,
    *,
    candidate_commit: str,
    candidate_tree: str,
) -> tuple[ExternalSignerConformanceReceipt, AgentGateAuthorityManifest]:
    manifest, private_key_path = _manifest_with_test_custody_key(tmp_path)
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
        private_key_path=private_key_path,
        repository_root=REPOSITORY_ROOT,
        authority=next(
            authority
            for authority in manifest.authorities
            if authority.role == "SEALED_CUSTODY"
        ),
    )
    receipt = ExternalSignerConformanceReceipt.model_validate(
        {**unsigned, "authority_signature": signature.model_dump(mode="json")}
    )
    return receipt, manifest


def test_external_signer_conformance_receipt_verifies_exact_external_snapshot(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "candidate-repository"
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)
    candidate_tree = _git(repository, "show", "-s", "--format=%T", "HEAD")
    receipt, manifest = _signed_receipt(
        tmp_path,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    receipt_path = tmp_path / "external-signer-conformance.json"
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")

    verified, receipt_sha256 = verify_external_signer_conformance_receipt(
        receipt_path=receipt_path,
        repository_root=repository,
        manifest=manifest,
        expected_candidate_commit=receipt.candidate_commit,
        expected_candidate_tree=receipt.candidate_tree,
        expected_authority_policy_sha256=receipt.authority_policy_sha256,
        expected_signer_bundle_sha256=receipt.signer_bundle_sha256,
    )

    assert verified == receipt
    assert receipt_sha256 == hashlib.sha256(receipt_path.read_bytes()).hexdigest()


def test_external_signer_conformance_rejects_binding_and_signature_tampering(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "candidate-repository"
    _write_minimal_protocol_schema_contract(repository)
    candidate_commit = _commit_minimal_repository(repository)
    candidate_tree = _git(repository, "show", "-s", "--format=%T", "HEAD")
    receipt, manifest = _signed_receipt(
        tmp_path,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    receipt_path = tmp_path / "external-signer-conformance.json"
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")

    with pytest.raises(ExternalAuthorityError, match="binding mismatch"):
        verify_external_signer_conformance_receipt(
            receipt_path=receipt_path,
            repository_root=repository,
            manifest=manifest,
            expected_candidate_commit="0" * 40,
            expected_candidate_tree=receipt.candidate_tree,
            expected_authority_policy_sha256=receipt.authority_policy_sha256,
            expected_signer_bundle_sha256=receipt.signer_bundle_sha256,
        )

    tampered = receipt.model_dump(mode="json")
    tampered["broker_id"] = "BROKER-EXTERNAL-0002"
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExternalAuthorityError, match="signature is invalid"):
        verify_external_signer_conformance_receipt(
            receipt_path=receipt_path,
            repository_root=repository,
            manifest=manifest,
            expected_candidate_commit=receipt.candidate_commit,
            expected_candidate_tree=receipt.candidate_tree,
            expected_authority_policy_sha256=receipt.authority_policy_sha256,
            expected_signer_bundle_sha256=receipt.signer_bundle_sha256,
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

    receipt_data = ExternalSignerConformanceReceipt.model_construct(
        supported_operations=OPERATIONS
    ).model_dump(mode="json", exclude_unset=True)
    assert receipt_data == {"supported_operations": OPERATIONS}
    with pytest.raises(ValueError, match="operation surface is incomplete"):
        ExternalSignerConformanceReceipt.model_validate(
            {
                **_minimal_receipt_payload(),
                "supported_operations": [*OPERATIONS[:-1], OPERATIONS[0]],
            }
        )


def _minimal_receipt_payload() -> dict[str, object]:
    return {
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
        "sanitized_environment_keys_sha256": "d" * 64,
        "inherited_handle_manifest_sha256": "e" * 64,
        "child_process_manifest_sha256": "f" * 64,
        "started_at": datetime(2026, 8, 28, 10, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 8, 28, 10, 1, tzinfo=UTC),
        "authority_signature": {
            "authority_role": "SEALED_CUSTODY",
            "authority_id": "AUTH-TEST-CUSTODY-000001",
            "algorithm": "ED25519",
            "signed_payload_sha256": "0" * 64,
            "signature_base64": "A" * 88,
        },
    }


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
    assert not {"payload", "role", "verdict", "aggregate"}.intersection(parameters)
