from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from evals.agent_gate_v1 import candidate_gate
from evals.agent_gate_v1.candidate_gate import (
    CandidateGateError,
    verify_g07_candidate_gate_pass,
)
from evals.agent_gate_v1.contracts import CurrentGoalBinding
from scripts import build_agent_gate_pass


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _candidate_repository(
    tmp_path: Path,
    *,
    isolation_mode: str = "FRESH_CLEAN_CHECKOUT",
) -> tuple[Path, Path, str, str, str, str, str]:
    root = tmp_path / "candidate"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "gate@example.test")
    _git(root, "config", "user.name", "Gate Test")
    _git(root, "checkout", "-b", "codex/trip-check-product-reset")
    agents = b"# G07 guidance\n"
    (root / "AGENTS.md").write_bytes(agents)
    (root / "docs/governance").mkdir(parents=True)
    (root / "backend/eval_data").mkdir(parents=True)
    (root / "backend/eval_data/value.json").write_text("{}\n", encoding="utf-8")
    (root / "backend/app").mkdir(parents=True)
    (root / "backend/app/value.py").write_text("VALUE = 1\n", encoding="utf-8")
    isolation: dict[str, object] = {
        "mode": "FRESH_CLEAN_CHECKOUT",
        "network_access": False,
        "synthetic_profile": False,
        "authority_secret_mount_count": 0,
    }
    if isolation_mode == "OCI_EPHEMERAL_NO_HOST_MOUNTS":
        isolation = {
            "mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
            "runner_recipe_path": (
                "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile"
            ),
            "runner_recipe_sha256": "1" * 64,
            "runner_entrypoint_path": (
                "backend/eval_data/agent_gate_v1/automation_runner_entrypoint.sh"
            ),
            "runner_entrypoint_sha256": "2" * 64,
            "runner_context_policy_path": (
                "backend/eval_data/agent_gate_v1/"
                "automation_runner.Dockerfile.dockerignore"
            ),
            "runner_context_policy_sha256": "3" * 64,
            "network_access": False,
            "host_mount_count": 0,
            "host_pid_namespace": False,
            "synthetic_profile": True,
            "authority_secret_mount_count": 0,
        }
    gate = {
        "schema_version": "automated-product-gate-contract-v2",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "gate_profile": "HARDENED_CANDIDATE_GATE",
        "isolation": isolation,
        "checks": [
            {
                "check_id": "g07.check",
                "argv": ["python", "-m", "pytest", "-q"],
                "workdir": "backend",
                "timeout_seconds": 60,
            }
        ],
    }
    _write_json(root / "gate.json", gate)
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    registry = {
        "active_goal_sequence": 7,
        "active_goal_id": "TC-VNEXT-G07-CANDIDATE",
        "mainline_phase": "CANDIDATE_HARDENING",
        "gate_profile": "HARDENED_CANDIDATE_GATE",
        "guidance_sha256": hashlib.sha256(agents).hexdigest(),
        "packages": [
            {
                "package_id": "WP-G07-INTEGRATOR",
                "goal_id": "TC-VNEXT-G07-CANDIDATE",
                "baseline_commit": baseline,
                "branch": "codex/trip-check-product-reset",
                "role": "INTEGRATOR",
                "dependencies": [],
                "owned_paths": ["backend", "docs"],
                "forbidden_paths": [],
                "acceptance": ["candidate gate passes"],
                "merge_order": 1,
                "status": "IN_PROGRESS",
            }
        ],
    }
    _write_json(root / "docs/governance/current_work_packages.json", registry)
    binding = {
        "schema_version": "current-goal-binding-v2",
        "goal_sequence": 7,
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "status": "IN_PROGRESS",
        "predecessor_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
        "predecessor_completion_commit": baseline,
        "automated_gate_contract_path": "gate.json",
        "automated_gate_contract_sha256": hashlib.sha256(
            (root / "gate.json").read_bytes()
        ).hexdigest(),
        "gate_profile": "HARDENED_CANDIDATE_GATE",
        "mainline_phase": "CANDIDATE_HARDENING",
        "work_package_registry_path": "docs/governance/current_work_packages.json",
    }
    _write_json(root / "docs/governance/current_goal_binding.json", binding)
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "activate G07")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "show", "-s", "--format=%T", "HEAD")
    config_sha = candidate_gate._git_bundle_sha256(
        root, commit, candidate_gate.CORE_CONFIG_ROOTS
    )
    data_sha = candidate_gate._git_bundle_sha256(
        root, commit, candidate_gate.CORE_DATA_ROOTS
    )
    development = tmp_path / "development"
    subprocess.run(
        ["git", "clone", "--quiet", str(root), str(development)],
        check=True,
    )
    return (
        root,
        development,
        commit,
        tree,
        config_sha,
        data_sha,
        binding["automated_gate_contract_sha256"],
    )


def _component(
    component: str,
    commit: str,
    tree: str,
    config_sha: str,
    data_sha: str,
    contract_sha: str,
    *,
    isolation: str | None = None,
) -> dict[str, object]:
    levels = {
        "AUTOMATED_PRODUCT_GATE": "AUTOMATED_TEST",
        "LIVE_PROVIDER_GATE": "LIVE_PROVIDER_EVIDENCE",
        "MULTI_AGENT_PANEL": "MULTI_AGENT_SIMULATED_REVIEW",
        "SEALED_AGENT_BLIND": "SEALED_AGENT_BLIND",
    }
    return {
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "candidate_config_sha256": config_sha,
        "candidate_data_sha256": data_sha,
        "automated_gate_contract_sha256": contract_sha,
        "component": component,
        "evidence_level": levels[component],
        "upstream_artifact_sha256": {"evidence.bundle": "1" * 64},
        "verifier_sha256": "2" * 64,
        "isolation_mode": isolation,
    }


def test_g07_not_required_uses_fresh_checkout_without_control_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        root,
        development,
        commit,
        tree,
        config_sha,
        data_sha,
        contract_sha,
    ) = _candidate_repository(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    components: list[Path] = []
    for component in (
        "AUTOMATED_PRODUCT_GATE",
        "LIVE_PROVIDER_GATE",
        "MULTI_AGENT_PANEL",
        "SEALED_AGENT_BLIND",
    ):
        path = external / f"{component}.json"
        _write_json(
            path,
            _component(
                component,
                commit,
                tree,
                config_sha,
                data_sha,
                contract_sha,
                isolation=(
                    "FRESH_CLEAN_CHECKOUT"
                    if component == "AUTOMATED_PRODUCT_GATE"
                    else None
                ),
            ),
        )
        components.append(path)
    decision = external / "decision.json"
    _write_json(
        decision,
        {
            "candidate_commit": commit,
            "candidate_tree": tree,
            "threat_model_sha256": "3" * 64,
            "decision": "NOT_REQUIRED_WITH_RATIONALE",
            "identified_threats": ["candidate evidence may drift"],
            "selected_controls": [],
            "alternative_controls": ["fresh checkout and remote readback"],
            "residual_risks": ["no organizational independence claim"],
            "rationale": "The bounded candidate threat model does not need external custody.",
        },
    )
    monkeypatch.setattr(
        candidate_gate,
        "_read_remote_candidate",
        lambda *_args: ("refs/heads/codex/trip-check-product-reset", commit, tree),
    )
    receipt = verify_g07_candidate_gate_pass(
        repository_root=root,
        development_checkout_root=development,
        expected_candidate_commit=commit,
        expected_candidate_tree=tree,
        component_receipt_paths=components,
        hardening_decision_path=decision,
        hardening_control_receipt_paths={},
        output_path=external / "pass.json",
    )
    assert receipt.hardening_decision == "NOT_REQUIRED_WITH_RATIONALE"
    assert receipt.selected_control_receipt_sha256 == {}


def test_g07_required_validates_only_selected_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        root,
        development,
        commit,
        tree,
        config_sha,
        data_sha,
        contract_sha,
    ) = _candidate_repository(
        tmp_path,
        isolation_mode="OCI_EPHEMERAL_NO_HOST_MOUNTS",
    )
    external = tmp_path / "external-required"
    external.mkdir()
    components: list[Path] = []
    for component in (
        "AUTOMATED_PRODUCT_GATE",
        "LIVE_PROVIDER_GATE",
        "MULTI_AGENT_PANEL",
        "SEALED_AGENT_BLIND",
    ):
        path = external / f"{component}.json"
        _write_json(
            path,
            _component(
                component,
                commit,
                tree,
                config_sha,
                data_sha,
                contract_sha,
                isolation=(
                    "OCI_EPHEMERAL_NO_HOST_MOUNTS"
                    if component == "AUTOMATED_PRODUCT_GATE"
                    else None
                ),
            ),
        )
        components.append(path)
    decision = external / "decision.json"
    _write_json(
        decision,
        {
            "candidate_commit": commit,
            "candidate_tree": tree,
            "threat_model_sha256": "3" * 64,
            "decision": "REQUIRED",
            "identified_threats": ["runner contamination"],
            "selected_controls": ["ISOLATED_OCI"],
            "alternative_controls": ["clean checkout alone was insufficient"],
            "residual_risks": ["process isolation is not human independence"],
            "rationale": "The measured runner threat requires only isolated OCI execution.",
        },
    )
    control = external / "oci.json"
    _write_json(
        control,
        {
            "candidate_commit": commit,
            "candidate_tree": tree,
            "control": "ISOLATED_OCI",
            "verifier_sha256": "4" * 64,
            "evidence_sha256": {"oci.execution": "5" * 64},
        },
    )
    monkeypatch.setattr(
        candidate_gate,
        "_read_remote_candidate",
        lambda *_args: ("refs/heads/codex/trip-check-product-reset", commit, tree),
    )
    wrong_decision = external / "wrong-decision.json"
    _write_json(
        wrong_decision,
        {
            "candidate_commit": commit,
            "candidate_tree": tree,
            "threat_model_sha256": "6" * 64,
            "decision": "NOT_REQUIRED_WITH_RATIONALE",
            "identified_threats": ["runner contamination was not confirmed"],
            "selected_controls": [],
            "alternative_controls": ["fresh clean checkout"],
            "residual_risks": ["no organizational independence claim"],
            "rationale": "This contradictory decision must not accept an OCI contract.",
        },
    )
    with pytest.raises(
        CandidateGateError,
        match="automated contract contradicts selected controls",
    ):
        verify_g07_candidate_gate_pass(
            repository_root=root,
            development_checkout_root=development,
            expected_candidate_commit=commit,
            expected_candidate_tree=tree,
            component_receipt_paths=components,
            hardening_decision_path=wrong_decision,
            hardening_control_receipt_paths={},
            output_path=external / "wrong-pass.json",
        )
    receipt = verify_g07_candidate_gate_pass(
        repository_root=root,
        development_checkout_root=development,
        expected_candidate_commit=commit,
        expected_candidate_tree=tree,
        component_receipt_paths=components,
        hardening_decision_path=decision,
        hardening_control_receipt_paths={"ISOLATED_OCI": control},
        output_path=external / "pass.json",
    )
    assert set(receipt.selected_control_receipt_sha256) == {"ISOLATED_OCI"}
    with pytest.raises(CandidateGateError, match="differs from decision"):
        verify_g07_candidate_gate_pass(
            repository_root=root,
            development_checkout_root=development,
            expected_candidate_commit=commit,
            expected_candidate_tree=tree,
            component_receipt_paths=components,
            hardening_decision_path=decision,
            hardening_control_receipt_paths={},
            output_path=external / "second-pass.json",
        )


def test_public_gate_entry_dispatches_g07_without_legacy_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = CurrentGoalBinding(
        schema_version="current-goal-binding-v2",
        goal_sequence=7,
        goal_id="TC-VNEXT-G07-CANDIDATE",
        status="IN_PROGRESS",
        predecessor_goal_id="TC-VNEXT-G06-MEMORY-SHARE",
        predecessor_completion_commit="1" * 40,
        automated_gate_contract_path="gate.json",
        automated_gate_contract_sha256="2" * 64,
        gate_profile="HARDENED_CANDIDATE_GATE",
        mainline_phase="CANDIDATE_HARDENING",
        work_package_registry_path="docs/governance/current_work_packages.json",
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(build_agent_gate_pass, "read_worktree_binding", lambda _root: binding)
    monkeypatch.setattr(
        build_agent_gate_pass,
        "verify_g07_candidate_gate_pass",
        lambda **kwargs: observed.update(kwargs),
    )
    argv = [
        "build_agent_gate_pass.py",
        "--development-checkout",
        str(tmp_path),
        "--candidate-commit",
        "3" * 40,
        "--candidate-tree",
        "4" * 40,
        "--output",
        str(tmp_path / "pass.json"),
        "--hardening-decision",
        str(tmp_path / "decision.json"),
    ]
    for index in range(4):
        argv.extend(["--component", str(tmp_path / f"component-{index}.json")])
    monkeypatch.setattr("sys.argv", argv)
    assert build_agent_gate_pass.main() == 0
    assert observed["hardening_control_receipt_paths"] == {}
