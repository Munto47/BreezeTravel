from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from evals.agent_gate_v1.candidate_component_verifiers import (
    CandidateComponentVerificationError,
    verify_candidate_component_receipt,
)
from evals.agent_gate_v1.contracts import (
    AutomatedProductGateContract,
    CandidateGateComponentReceipt,
    CurrentGoalBinding,
    G07CandidateGatePassReceipt,
    HardeningControl,
    HardeningControlVerificationReceipt,
    HardeningDecisionReceipt,
)
from evals.agent_gate_v1.core_gate import CORE_CONFIG_ROOTS, CORE_DATA_ROOTS
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)
from evals.agent_gate_v1.work_packages import (
    WorkPackageValidationError,
    load_candidate_work_package_registry,
)
from governance.work_packages_v3 import validate_registry_v3


class CandidateGateError(ValueError):
    pass


G07_THREAT_MODEL_PATH = "backend/eval_data/g07_candidate/threat_model_v1.json"


def _git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=not binary,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if binary
            else result.stderr
        )
        raise CandidateGateError(
            f"Git candidate readback failed: {' '.join(args)}: {str(stderr).strip()}"
        )
    return result.stdout if binary else result.stdout.strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_blob(root: Path, commit: str, path: str) -> bytes:
    value = _git(root, "show", f"{commit}:{path}", binary=True)
    assert isinstance(value, bytes)
    return value


def _git_blob_sha256(root: Path, commit: str, path: str) -> str:
    return _sha256_bytes(_git_blob(root, commit, path))


def _git_bundle_sha256(root: Path, commit: str, roots: tuple[str, ...]) -> str:
    value = _git(
        root,
        "ls-tree",
        "-r",
        "--full-tree",
        commit,
        "--",
        *roots,
        binary=True,
    )
    assert isinstance(value, bytes)
    entries: list[list[str]] = []
    for raw in value.splitlines():
        metadata, raw_path = raw.split(b"\t", maxsplit=1)
        _mode, object_type, object_id = metadata.decode("ascii").split()
        if object_type == "blob":
            entries.append([raw_path.decode("utf-8"), object_type, object_id])
    if not entries:
        raise CandidateGateError("candidate Git bundle resolved no blobs")
    return _sha256_bytes(
        json.dumps(
            sorted(entries),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _load_external_model(path: Path, model_type: type, repository_root: Path):
    snapshot = read_external_snapshot(path, repository_root)
    try:
        value = model_type.model_validate_json(snapshot.content)
    except ValueError as exc:
        raise CandidateGateError(f"invalid external candidate receipt: {path.name}") from exc
    return snapshot, value


def _read_remote_candidate(
    root: Path,
    binding: CurrentGoalBinding,
    candidate_commit: str,
    candidate_tree: str,
) -> tuple[str, str, str]:
    origin = str(_git(root, "remote", "get-url", "origin"))
    if origin != "https://github.com/Munto47/BreezeTravel.git":
        raise CandidateGateError("G07 checkout is not bound to the canonical origin")
    remote_ref = binding.canonical_candidate_ref
    lines = str(_git(root, "ls-remote", "--refs", "origin", remote_ref)).splitlines()
    if len(lines) != 1:
        raise CandidateGateError("G07 remote ref did not resolve exactly once")
    subject, observed_ref = lines[0].split(maxsplit=1)
    if observed_ref != remote_ref or subject != candidate_commit:
        raise CandidateGateError("G07 remote subject does not match candidate")
    _git(root, "fetch", "--no-tags", "origin", remote_ref)
    tree = str(_git(root, "show", "-s", "--format=%T", subject))
    if tree != candidate_tree:
        raise CandidateGateError("G07 remote tree does not match candidate")
    return remote_ref, subject, tree


def _candidate_contract_binding(binding: CurrentGoalBinding) -> tuple[str, str]:
    if binding.schema_version == "current-goal-binding-v3":
        path = binding.candidate_gate_contract_path
        sha256 = binding.candidate_gate_contract_sha256
    else:
        path = binding.automated_gate_contract_path
        sha256 = binding.automated_gate_contract_sha256
    if path is None or sha256 is None:
        raise CandidateGateError("G07 candidate contract binding is incomplete")
    return path, sha256


def verify_g07_candidate_gate_pass(
    *,
    repository_root: Path,
    development_checkout_root: Path,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    component_receipt_paths: list[Path],
    hardening_decision_path: Path,
    hardening_control_receipt_paths: dict[HardeningControl, Path],
    output_path: Path,
) -> G07CandidateGatePassReceipt:
    root = repository_root.resolve(strict=True)
    development_root = development_checkout_root.resolve(strict=True)
    if root == development_root:
        raise CandidateGateError("G07 Gate requires a distinct clean checkout")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CandidateGateError("G07 candidate checkout is not clean")
    if _git(development_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CandidateGateError("G07 development checkout is not clean")
    if _git(root, "rev-parse", "HEAD") != expected_candidate_commit:
        raise CandidateGateError("G07 checkout HEAD does not match candidate")
    if _git(root, "show", "-s", "--format=%T", "HEAD") != expected_candidate_tree:
        raise CandidateGateError("G07 checkout tree does not match candidate")

    binding_path = "docs/governance/current_goal_binding.json"
    try:
        binding = CurrentGoalBinding.model_validate_json(
            _git_blob(root, expected_candidate_commit, binding_path)
        )
    except ValueError as exc:
        raise CandidateGateError("invalid G07 current Goal binding") from exc
    if (
        binding.schema_version not in {
            "current-goal-binding-v2",
            "current-goal-binding-v3",
        }
        or binding.goal_sequence != 7
        or binding.goal_id != "TC-VNEXT-G07-CANDIDATE"
        or binding.gate_profile != "HARDENED_CANDIDATE_GATE"
    ):
        raise CandidateGateError("G07 Gate requires the active v2 or v3 G07 binding")
    contract_path, expected_contract_sha256 = _candidate_contract_binding(binding)
    contract_sha256 = _git_blob_sha256(
        root, expected_candidate_commit, contract_path
    )
    if contract_sha256 != expected_contract_sha256:
        raise CandidateGateError("G07 automated contract hash mismatch")
    try:
        automated_contract = AutomatedProductGateContract.model_validate_json(
            _git_blob(
                root,
                expected_candidate_commit,
                contract_path,
            )
        )
    except ValueError as exc:
        raise CandidateGateError("invalid G07 automated contract") from exc
    if (
        automated_contract.goal_id != binding.goal_id
        or automated_contract.gate_profile != binding.gate_profile
    ):
        raise CandidateGateError("G07 automated contract disagrees with binding")
    if binding.schema_version == "current-goal-binding-v3":
        registry_report = validate_registry_v3(
            root,
            check_scope=False,
            head_ref=expected_candidate_commit,
            require_all_merged=True,
        )
        if registry_report["verdict"] != "PASS":
            joined = ",".join(registry_report["error_codes"])
            raise CandidateGateError(
                f"invalid G07 work package governance: {joined}"
            )
        assert binding.work_package_registry_path is not None
        registry_sha256 = _git_blob_sha256(
            root,
            expected_candidate_commit,
            binding.work_package_registry_path,
        )
    else:
        try:
            _registry, registry_sha256 = load_candidate_work_package_registry(
                root,
                expected_candidate_commit,
                binding,
                require_gate_ready=True,
            )
        except WorkPackageValidationError as exc:
            raise CandidateGateError(
                f"invalid G07 work package governance: {exc}"
            ) from exc

    config_sha256 = _git_bundle_sha256(root, expected_candidate_commit, CORE_CONFIG_ROOTS)
    data_sha256 = _git_bundle_sha256(root, expected_candidate_commit, CORE_DATA_ROOTS)
    decision_snapshot, decision = _load_external_model(
        hardening_decision_path,
        HardeningDecisionReceipt,
        root,
    )
    if (
        decision.candidate_commit != expected_candidate_commit
        or decision.candidate_tree != expected_candidate_tree
    ):
        raise CandidateGateError("hardening decision is bound to another candidate")
    threat_model_bytes = _git_blob(
        root,
        expected_candidate_commit,
        G07_THREAT_MODEL_PATH,
    )
    if decision.threat_model_sha256 != _sha256_bytes(threat_model_bytes):
        raise CandidateGateError("hardening decision threat model binding mismatch")
    try:
        threat_model = json.loads(threat_model_bytes)
    except json.JSONDecodeError as exc:
        raise CandidateGateError("invalid G07 threat model") from exc
    if (
        threat_model.get("schema_version") != "g07-candidate-threat-model-v1"
        or threat_model.get("goal_id") != binding.goal_id
    ):
        raise CandidateGateError("G07 threat model disagrees with candidate binding")

    expected_components = {
        "AUTOMATED_PRODUCT_GATE",
        "LIVE_PROVIDER_GATE",
        "MULTI_AGENT_PANEL",
        "SEALED_AGENT_BLIND",
    }
    component_hashes: dict[str, str] = {}
    automated_isolation: str | None = None
    for path in component_receipt_paths:
        snapshot, receipt = _load_external_model(path, CandidateGateComponentReceipt, root)
        if receipt.component in component_hashes:
            raise CandidateGateError("duplicate G07 candidate component")
        if (
            receipt.candidate_commit != expected_candidate_commit
            or receipt.candidate_tree != expected_candidate_tree
            or receipt.candidate_config_sha256 != config_sha256
            or receipt.candidate_data_sha256 != data_sha256
            or receipt.automated_gate_contract_sha256 != contract_sha256
        ):
            raise CandidateGateError("G07 component candidate binding mismatch")
        try:
            verify_candidate_component_receipt(
                receipt=receipt,
                repository_root=root,
            )
        except CandidateComponentVerificationError as exc:
            raise CandidateGateError(
                f"G07 component raw verification failed: {receipt.component}"
            ) from exc
        component_hashes[receipt.component] = snapshot.sha256
        if receipt.component == "AUTOMATED_PRODUCT_GATE":
            automated_isolation = receipt.isolation_mode
    if set(component_hashes) != expected_components:
        raise CandidateGateError("G07 candidate component set is incomplete")

    selected = set(decision.selected_controls)
    if set(hardening_control_receipt_paths) != selected:
        raise CandidateGateError("hardening control receipt set differs from decision")
    control_hashes: dict[str, str] = {}
    for control, path in hardening_control_receipt_paths.items():
        snapshot, receipt = _load_external_model(
            path,
            HardeningControlVerificationReceipt,
            root,
        )
        if (
            receipt.control != control
            or receipt.candidate_commit != expected_candidate_commit
            or receipt.candidate_tree != expected_candidate_tree
        ):
            raise CandidateGateError("hardening control candidate binding mismatch")
        control_hashes[control] = snapshot.sha256
    expected_isolation = (
        "OCI_EPHEMERAL_NO_HOST_MOUNTS"
        if "ISOLATED_OCI" in selected
        else "FRESH_CLEAN_CHECKOUT"
    )
    if automated_contract.isolation.mode != expected_isolation:
        raise CandidateGateError(
            "G07 automated contract contradicts selected controls"
        )
    if automated_isolation != expected_isolation:
        raise CandidateGateError("G07 automation isolation contradicts selected controls")

    remote_ref, remote_subject, remote_tree = _read_remote_candidate(
        root,
        binding,
        expected_candidate_commit,
        expected_candidate_tree,
    )
    receipt = G07CandidateGatePassReceipt(
        candidate_commit=expected_candidate_commit,
        candidate_tree=expected_candidate_tree,
        current_goal_binding_sha256=_git_blob_sha256(
            root, expected_candidate_commit, binding_path
        ),
        work_package_registry_sha256=registry_sha256,
        candidate_config_sha256=config_sha256,
        candidate_data_sha256=data_sha256,
        hardening_decision=decision.decision,
        hardening_decision_receipt_sha256=decision_snapshot.sha256,
        component_receipt_sha256=component_hashes,
        selected_control_receipt_sha256=control_hashes,
        remote_ref=remote_ref,
        remote_subject=remote_subject,
        remote_tree=remote_tree,
        completed_at=datetime.now(timezone.utc),
    )
    content = (
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    write_external_bytes_exclusive(output_path, content, root)
    return receipt
