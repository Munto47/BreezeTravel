from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from evals.agent_gate_v1.authority import (
    candidate_freeze_ref,
    compute_git_blob_bundle_hash,
    compute_public_key_set_sha256,
    git_blob_sha256,
    load_anchored_authority_policy,
    load_current_goal_binding,
    load_current_goal_document_state,
    require_formal_origin,
)
from evals.agent_gate_v1.custody import (
    load_registered_authority_anchor,
    register_goal_gate_pass,
    require_predecessor_goal_pass,
)
from evals.agent_gate_v1 import external_authority as _external_authority
from evals.agent_gate_v1.component_verifiers import (
    load_strict_component_receipt,
    verify_strict_component_receipt,
)
from evals.agent_gate_v1.contracts import (
    AgentGateComponent,
    AgentGatePassReceipt,
    DetachedAuthoritySignature,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.path_security import (
    discover_repository_root,
    require_external_existing,
    require_external_target,
    write_external_bytes_exclusive,
)
from evals.agent_gate_v1.signing import verify_payload_signature
from evals.trip_text_cards_v1 import contracts as _trip_text_cards_v1_contracts
from evals.trip_text_cards_v1 import scorer as _trip_text_cards_v1_scorer


class AgentGatePassError(ValueError):
    pass


REQUIRED_COMPONENTS: set[AgentGateComponent] = {
    "AUTOMATED_PRODUCT_GATE",
    "LIVE_PROVIDER_GATE",
    "MULTI_AGENT_PANEL",
    "SEALED_AGENT_BLIND",
}

RUNTIME_MODULE_PATHS = {
    "evals.agent_gate_v1.authority": "backend/evals/agent_gate_v1/authority.py",
    "evals.agent_gate_v1.automation_isolation": (
        "backend/evals/agent_gate_v1/automation_isolation.py"
    ),
    "evals.agent_gate_v1.component_verifiers": (
        "backend/evals/agent_gate_v1/component_verifiers.py"
    ),
    "evals.agent_gate_v1.contracts": "backend/evals/agent_gate_v1/contracts.py",
    "evals.agent_gate_v1.custody": "backend/evals/agent_gate_v1/custody.py",
    "evals.agent_gate_v1.final_gate": "backend/evals/agent_gate_v1/final_gate.py",
    _external_authority.__name__: (
        "backend/evals/agent_gate_v1/external_authority.py"
    ),
    "evals.agent_gate_v1.host_tools": "backend/evals/agent_gate_v1/host_tools.py",
    "evals.agent_gate_v1.path_security": (
        "backend/evals/agent_gate_v1/path_security.py"
    ),
    "evals.agent_gate_v1.signing": "backend/evals/agent_gate_v1/signing.py",
    "evals.agent_gate_v1.validator": "backend/evals/agent_gate_v1/validator.py",
    "evals.agent_gate_v1.sealed_score": (
        "backend/evals/agent_gate_v1/sealed_score.py"
    ),
    "evals.trip_text_cards_agent_v2.annotations": (
        "backend/evals/trip_text_cards_agent_v2/annotations.py"
    ),
    "evals.trip_text_cards_agent_v2.contracts": (
        "backend/evals/trip_text_cards_agent_v2/contracts.py"
    ),
    _trip_text_cards_v1_contracts.__name__: (
        "backend/evals/trip_text_cards_v1/contracts.py"
    ),
    _trip_text_cards_v1_scorer.__name__: (
        "backend/evals/trip_text_cards_v1/scorer.py"
    ),
}


def _git(root: Path, *args: str, text: bool = True):
    try:
        result = subprocess.run(
            [trusted_host_tool("git"), "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=text,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentGatePassError(
            f"Git final Gate readback timed out: {' '.join(args)}"
        ) from exc
    if result.returncode != 0:
        raise AgentGatePassError(f"Git final Gate readback failed: {' '.join(args)}")
    return result.stdout.strip() if text else result.stdout


def _verify_runtime_provenance(
    *,
    repository_root: Path,
    candidate_commit: str,
) -> str:
    try:
        _external_authority.verify_candidate_protocol_schema_bindings(
            repository_root=repository_root,
            candidate_commit=candidate_commit,
        )
    except _external_authority.ExternalAuthorityError as exc:
        raise AgentGatePassError(
            "candidate protocol schema provenance is invalid"
        ) from exc
    verifier_sha256 = ""
    for module_name, relative_path in RUNTIME_MODULE_PATHS.items():
        module = sys.modules.get(module_name)
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            raise AgentGatePassError(f"required Gate module is not loaded: {module_name}")
        expected_path = (repository_root / relative_path).resolve(strict=True)
        if Path(module_file).resolve(strict=True) != expected_path:
            raise AgentGatePassError(
                f"Gate module was not loaded from the candidate checkout: {module_name}"
            )
        candidate_blob = _git(
            repository_root,
            "show",
            f"{candidate_commit}:{relative_path}",
            text=False,
        )
        observed_sha256 = hashlib.sha256(expected_path.read_bytes()).hexdigest()
        candidate_sha256 = hashlib.sha256(candidate_blob).hexdigest()
        if observed_sha256 != candidate_sha256:
            raise AgentGatePassError(
                f"running Gate module is not candidate-bound: {module_name}"
            )
        if module_name == "evals.agent_gate_v1.final_gate":
            verifier_sha256 = candidate_sha256
    if not verifier_sha256:
        raise AgentGatePassError("final Gate verifier provenance was not established")
    return verifier_sha256


def _read_remote_candidate(
    *,
    root: Path,
    remote_ref: str,
    expected_commit: str,
    expected_tree: str,
) -> tuple[str, str]:
    remote_lines = _git(root, "ls-remote", "--refs", "origin", remote_ref).splitlines()
    if len(remote_lines) != 1:
        raise AgentGatePassError("immutable candidate ref did not resolve exactly once")
    remote_subject, observed_ref = remote_lines[0].split(maxsplit=1)
    if observed_ref != remote_ref or remote_subject != expected_commit:
        raise AgentGatePassError("immutable candidate ref does not match the candidate")
    _git(root, "fetch", "--no-tags", "origin", remote_ref)
    remote_tree = _git(root, "show", "-s", "--format=%T", remote_subject)
    if remote_tree != expected_tree:
        raise AgentGatePassError("immutable candidate tree does not match the candidate")
    return remote_subject, remote_tree


def verify_agent_gate_pass(
    *,
    component_receipt_paths: list[Path],
    development_checkout_root: Path,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    completed_at: datetime,
    authority_signature: DetachedAuthoritySignature,
    custody_registry_path: Path,
) -> AgentGatePassReceipt:
    if len(component_receipt_paths) != 4:
        raise AgentGatePassError("AGENT_GATE_PASS requires exactly four strict components")
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise AgentGatePassError("external final-Gate completion time must be timezone-aware")
    fresh_root = discover_repository_root(Path(__file__).parent)
    development_root = development_checkout_root.resolve(strict=True)
    if fresh_root == development_root:
        raise AgentGatePassError(
            "final Gate must execute inside a checkout distinct from development"
        )
    if Path(_git(fresh_root, "rev-parse", "--show-toplevel")).resolve() != fresh_root:
        raise AgentGatePassError("fresh checkout root does not match Git top-level")
    if (
        Path(_git(development_root, "rev-parse", "--show-toplevel")).resolve()
        != development_root
    ):
        raise AgentGatePassError("development checkout root does not match Git top-level")
    if _git(fresh_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise AgentGatePassError("fresh checkout is not clean")
    if _git(fresh_root, "rev-parse", "HEAD") != expected_candidate_commit:
        raise AgentGatePassError("fresh checkout HEAD does not match the candidate")
    if _git(fresh_root, "show", "-s", "--format=%T", "HEAD") != expected_candidate_tree:
        raise AgentGatePassError("fresh checkout tree does not match the candidate")

    anchored = load_anchored_authority_policy(fresh_root, expected_candidate_commit)
    require_formal_origin(fresh_root, anchored.manifest)
    require_formal_origin(development_root, anchored.manifest)
    goal_binding = load_current_goal_binding(
        fresh_root,
        expected_candidate_commit,
        anchored.manifest,
    )
    expected_goal_id = goal_binding.goal_id
    _goal_state, current_goal_document_sha256 = load_current_goal_document_state(
        fresh_root,
        expected_candidate_commit,
        anchored.manifest,
        goal_binding,
    )
    require_predecessor_goal_pass(
        registry_path=custody_registry_path,
        repository_root=fresh_root,
        anchored_policy=anchored,
        current_binding=goal_binding,
    )
    remote_ref = candidate_freeze_ref(
        anchored.manifest,
        goal_binding.goal_sequence,
        expected_candidate_commit,
    )
    immutable_protocol_sha256 = compute_git_blob_bundle_hash(
        fresh_root,
        expected_candidate_commit,
        anchored.manifest.immutable_protocol_paths,
    )
    program_core_sha256 = compute_git_blob_bundle_hash(
        fresh_root,
        expected_candidate_commit,
        anchored.manifest.program_core_paths,
    )
    registered_anchor = load_registered_authority_anchor(
        registry_path=custody_registry_path,
        repository_root=fresh_root,
        manifest=anchored.manifest,
    )
    expected_anchor = (
        anchored.manifest.authority_generation,
        anchored.manifest.canonical_candidate_ref,
        anchored.anchor_commit,
        _git(fresh_root, "show", "-s", "--format=%T", anchored.anchor_commit),
        anchored.sha256,
        program_core_sha256,
        immutable_protocol_sha256,
        compute_public_key_set_sha256(anchored.manifest),
        anchored.manifest.custody_registry_identity_sha256,
    )
    observed_anchor = (
        registered_anchor.receipt.authority_generation,
        registered_anchor.receipt.canonical_candidate_ref,
        registered_anchor.receipt.anchor_commit,
        registered_anchor.receipt.anchor_tree,
        registered_anchor.receipt.authority_policy_sha256,
        registered_anchor.receipt.program_core_sha256,
        registered_anchor.receipt.immutable_protocol_sha256,
        registered_anchor.receipt.public_key_set_sha256,
        registered_anchor.receipt.custody_registry_identity_sha256,
    )
    if observed_anchor != expected_anchor:
        raise AgentGatePassError("external authority anchor disagrees with candidate history")
    verifier_sha256 = _verify_runtime_provenance(
        repository_root=fresh_root,
        candidate_commit=expected_candidate_commit,
    )

    config_sha256 = compute_git_blob_bundle_hash(
        fresh_root,
        expected_candidate_commit,
        anchored.manifest.config_roots,
    )
    data_sha256 = compute_git_blob_bundle_hash(
        fresh_root,
        expected_candidate_commit,
        anchored.manifest.data_roots,
    )

    remote_subject, remote_tree = _read_remote_candidate(
        root=fresh_root,
        remote_ref=remote_ref,
        expected_commit=expected_candidate_commit,
        expected_tree=expected_candidate_tree,
    )

    receipt_hashes: dict[AgentGateComponent, str] = {}
    for path in component_receipt_paths:
        # A receipt must remain outside both the clean verification checkout and
        # the mutable development checkout.  Role signatures then bind its
        # bytes to the candidate and its upstream evidence.
        require_external_existing(path, development_root)
        snapshot, receipt = load_strict_component_receipt(path, fresh_root)
        if receipt.component in receipt_hashes:
            raise AgentGatePassError("duplicate strict Agent Gate component")
        verify_strict_component_receipt(
            receipt=receipt,
            repository_root=fresh_root,
            anchored_policy=anchored,
            expected_goal_id=expected_goal_id,
            expected_candidate_commit=expected_candidate_commit,
            expected_candidate_tree=expected_candidate_tree,
            expected_config_sha256=config_sha256,
            expected_data_sha256=data_sha256,
            custody_registry_path=custody_registry_path,
        )
        receipt_hashes[receipt.component] = snapshot.sha256
    if set(receipt_hashes) != REQUIRED_COMPONENTS:
        raise AgentGatePassError("strict Agent Gate component set is incomplete")

    # The unique candidate ref is read again after every potentially lengthy
    # component verification and immediately before signing.
    remote_subject, remote_tree = _read_remote_candidate(
        root=fresh_root,
        remote_ref=remote_ref,
        expected_commit=expected_candidate_commit,
        expected_tree=expected_candidate_tree,
    )

    unsigned = {
        "schema_version": "agent-gate-pass-receipt-v2",
        "gate_profile": goal_binding.gate_profile,
        "goal_sequence": goal_binding.goal_sequence,
        "goal_id": expected_goal_id,
        "predecessor_goal_id": goal_binding.predecessor_goal_id,
        "predecessor_completion_commit": (
            goal_binding.predecessor_completion_commit
        ),
        "current_goal_binding_sha256": git_blob_sha256(
            fresh_root,
            expected_candidate_commit,
            anchored.manifest.current_goal_binding_path,
        ),
        "current_goal_document_sha256": current_goal_document_sha256,
        "automated_gate_contract_sha256": (
            goal_binding.automated_gate_contract_sha256
        ),
        "candidate_commit": expected_candidate_commit,
        "candidate_tree": expected_candidate_tree,
        "authority_anchor_commit": anchored.anchor_commit,
        "authority_policy_sha256": anchored.sha256,
        "authority_generation": anchored.manifest.authority_generation,
        "authority_anchor_receipt_sha256": registered_anchor.receipt_sha256,
        "canonical_origin_url": anchored.manifest.canonical_origin_url,
        "candidate_config_sha256": config_sha256,
        "candidate_data_sha256": data_sha256,
        "frozen_binding_sha256": {},
        "component_receipt_sha256": receipt_hashes,
        "fresh_checkout_root_sha256": hashlib.sha256(
            str(fresh_root).encode("utf-8")
        ).hexdigest(),
        "remote_name": "origin",
        "remote_ref": remote_ref,
        "remote_subject": remote_subject,
        "remote_tree": remote_tree,
        "verifier_sha256": verifier_sha256,
        "evidence_levels": [
            "AUTOMATED_TEST",
            "LIVE_PROVIDER_EVIDENCE",
            "MULTI_AGENT_SIMULATED_REVIEW",
            "SEALED_AGENT_BLIND",
        ],
        "human_usability_status": "NOT_RUN",
        "production_status": "NOT_RUN",
        "verdict": "AGENT_GATE_PASS",
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
    }
    try:
        verify_payload_signature(
            payload=unsigned,
            signature=authority_signature,
            manifest=anchored.manifest,
            expected_role="FINAL_GATE",
        )
    except ValueError as exc:
        raise AgentGatePassError("external final-Gate signature is invalid") from exc
    return AgentGatePassReceipt.model_validate(
        {
            **unsigned,
            "authority_signature": authority_signature.model_dump(mode="json"),
        }
    )


def write_agent_gate_pass_receipt(
    *,
    receipt: AgentGatePassReceipt,
    output: Path,
    repository_root: Path,
    development_checkout_root: Path,
    custody_registry_path: Path,
) -> None:
    content = (
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    # Materialize first; only a durable byte-identical receipt may authorize the
    # next Goal in the external registry. A retry may reuse the same file.
    if output.exists():
        existing = require_external_existing(output, repository_root)
        require_external_existing(output, development_checkout_root)
        if existing.read_bytes() != content:
            raise AgentGatePassError(
                "existing Agent Gate PASS output contains different bytes"
            )
    else:
        require_external_target(output, development_checkout_root)
        write_external_bytes_exclusive(output, content, repository_root)
    register_goal_gate_pass(
        registry_path=custody_registry_path,
        repository_root=repository_root,
        receipt=receipt,
        materialized_receipt_path=output,
    )
