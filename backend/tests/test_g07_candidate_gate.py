from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.agent_gate_v1 import candidate_gate
from evals.agent_gate_v1.candidate_gate import (
    CandidateGateError,
    verify_g07_candidate_gate_pass,
)
from evals.agent_gate_v1.contracts import CurrentGoalBinding
from scripts import build_agent_gate_pass


@pytest.fixture(autouse=True)
def _stub_component_raw_revalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        candidate_gate,
        "verify_candidate_component_receipt",
        lambda **_kwargs: {"verdict": "PASS"},
    )


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


def _threat_model_sha(root: Path, commit: str) -> str:
    content = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "show",
            f"{commit}:backend/eval_data/g07_candidate/threat_model_v1.json",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(content).hexdigest()


def _candidate_repository(
    tmp_path: Path,
    *,
    isolation_mode: str = "FRESH_CLEAN_CHECKOUT",
    binding_version: str = "current-goal-binding-v2",
) -> tuple[Path, Path, str, str, str, str, str]:
    root = tmp_path / "candidate"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "gate@example.test")
    _git(root, "config", "user.name", "Gate Test")
    branch = (
        "codex/g07-candidate"
        if binding_version == "current-goal-binding-v3"
        else "codex/trip-check-product-reset"
    )
    _git(root, "checkout", "-b", branch)
    agents = b"# G07 guidance\n"
    (root / "AGENTS.md").write_bytes(agents)
    (root / "docs/governance").mkdir(parents=True)
    (root / "backend/eval_data").mkdir(parents=True)
    (root / "backend/eval_data/value.json").write_text("{}\n", encoding="utf-8")
    _write_json(
        root / "backend/eval_data/g07_candidate/threat_model_v1.json",
        {
            "schema_version": "g07-candidate-threat-model-v1",
            "goal_id": "TC-VNEXT-G07-CANDIDATE",
        },
    )
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
    if binding_version == "current-goal-binding-v3":
        _write_json(root / "docs/governance/product_delivery_gates.json", {"version": 1})
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    registry: dict[str, object] = {
        "schema_version": (
            "work-package-registry-v3"
            if binding_version == "current-goal-binding-v3"
            else "work-package-registry-v2"
        ),
        "active_goal_sequence": 7,
        "active_goal_id": "TC-VNEXT-G07-CANDIDATE",
        "mainline_phase": "CANDIDATE_HARDENING",
        "gate_profile": "HARDENED_CANDIDATE_GATE",
        "guidance_sha256": hashlib.sha256(agents).hexdigest(),
        "integration_order": [],
        "e2e_after_all_merges": True,
        "packages": [
            {
                "package_id": "WP-G07-INTEGRATOR",
                "goal_id": "TC-VNEXT-G07-CANDIDATE",
                "baseline_commit": baseline,
                "branch": branch,
                "remote_branch": f"origin/{branch}",
                "worktree_path": root.resolve().as_posix(),
                "role": "INTEGRATOR",
                "execution_mode": "PRIMARY_INTEGRATOR_DIALOGUE",
                "dialogue_ref": "codex-task:primary",
                "dependencies": [],
                "owned_paths": ["backend", "docs"],
                "forbidden_paths": [],
                "acceptance": ["candidate gate passes"],
                "merge_order": 1,
                "status": "IN_PROGRESS",
            }
        ],
    }
    if binding_version == "current-goal-binding-v3":
        registry.update(
            {
                "program_id": "TC-VNEXT-2026",
                "program_state": "G07_CANDIDATE_IN_PROGRESS",
                "scope_guard_version": "core-mainline-v1",
                "scope_policy_sha256": hashlib.sha256(
                    (root / "docs/governance/product_delivery_gates.json").read_bytes()
                ).hexdigest(),
                "max_parallel_writers": 2,
                "active_slice": {
                    "slice_id": "G07-CANDIDATE-CONTRACT",
                    "work_kind": "CANDIDATE_HARDENING",
                    "allowed_paths": [
                        "backend/evals/agent_gate_v1/candidate_gate.py",
                        "backend/evals/agent_gate_v1/contracts.py",
                        "docs",
                    ],
                },
            }
        )
        (root / "docs/governance/CURRENT_GOAL.md").write_text(
            "# G07\n\n"
            "<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE\n"
            + json.dumps(
                {
                    "goal_id": "TC-VNEXT-G07-CANDIDATE",
                    "goal_status": "IN_PROGRESS",
                }
            )
            + "\n-->\n",
            encoding="utf-8",
        )
    _write_json(root / "docs/governance/current_work_packages.json", registry)
    binding: dict[str, object] = {
        "schema_version": binding_version,
        "goal_sequence": 7,
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "status": "IN_PROGRESS",
        "predecessor_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
        "predecessor_completion_commit": baseline,
        "automated_gate_contract_path": (
            "docs/governance/product_delivery_gates.json"
            if binding_version == "current-goal-binding-v3"
            else "gate.json"
        ),
        "automated_gate_contract_sha256": hashlib.sha256(
            (
                root / "docs/governance/product_delivery_gates.json"
                if binding_version == "current-goal-binding-v3"
                else root / "gate.json"
            ).read_bytes()
        ).hexdigest(),
        "gate_profile": "HARDENED_CANDIDATE_GATE",
        "mainline_phase": "CANDIDATE_HARDENING",
        "work_package_registry_path": "docs/governance/current_work_packages.json",
    }
    if binding_version == "current-goal-binding-v3":
        binding.update(
            {
                "program_id": "TC-VNEXT-2026",
                "canonical_candidate_ref": "refs/heads/codex/g07-candidate",
                "implementation_baseline_commit": baseline,
                "last_completed_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
                "next_goal_id": "TC-H1-G01-HUMAN-USABILITY",
                "next_goal_status": "REQUIRES_OWNER_APPROVAL",
                "program_state": "G07_CANDIDATE_IN_PROGRESS",
                "candidate_gate_contract_path": "gate.json",
                "candidate_gate_contract_sha256": hashlib.sha256(
                    (root / "gate.json").read_bytes()
                ).hexdigest(),
            }
        )
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
        (
            binding["candidate_gate_contract_sha256"]
            if binding_version == "current-goal-binding-v3"
            else binding["automated_gate_contract_sha256"]
        ),
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
        "schema_version": "candidate-gate-component-receipt-v2",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "candidate_config_sha256": config_sha,
        "candidate_data_sha256": data_sha,
        "automated_gate_contract_sha256": contract_sha,
        "component": component,
        "evidence_level": levels[component],
        "upstream_artifact_path": {
            "evidence.bundle": "C:/g07-external/evidence.bundle"
        },
        "upstream_artifact_sha256": {"evidence.bundle": "1" * 64},
        "verifier_path": (
            "backend/evals/agent_gate_v1/candidate_component_verifiers.py"
        ),
        "verifier_sha256": "2" * 64,
        "verification_summary_sha256": "3" * 64,
        "isolation_mode": isolation,
    }


def test_checked_in_v3_binding_selects_separate_candidate_contract() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    binding_path = repository_root / "docs/governance/current_goal_binding.json"
    binding = CurrentGoalBinding.model_validate_json(binding_path.read_text(encoding="utf-8"))

    contract_path, contract_sha256 = candidate_gate._candidate_contract_binding(binding)

    assert binding.schema_version == "current-goal-binding-v3"
    assert binding.automated_gate_contract_path == "docs/governance/product_delivery_gates.json"
    assert contract_path == "backend/eval_data/agent_gate_v1/g07_automated_product_gate.json"
    assert hashlib.sha256((repository_root / contract_path).read_bytes()).hexdigest() == (
        contract_sha256
    )


def test_g07_not_required_uses_fresh_checkout_without_control_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_components: list[str] = []
    monkeypatch.setattr(
        candidate_gate,
        "verify_candidate_component_receipt",
        lambda *, receipt, repository_root: verified_components.append(
            receipt.component
        )
        or {"verdict": "PASS"},
    )
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
            "threat_model_sha256": _threat_model_sha(root, commit),
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
    assert set(verified_components) == {
        "AUTOMATED_PRODUCT_GATE",
        "LIVE_PROVIDER_GATE",
        "MULTI_AGENT_PANEL",
        "SEALED_AGENT_BLIND",
    }
    drifted_decision = external / "drifted-decision.json"
    payload = json.loads(decision.read_text(encoding="utf-8"))
    payload["threat_model_sha256"] = "0" * 64
    _write_json(drifted_decision, payload)
    with pytest.raises(CandidateGateError, match="threat model binding mismatch"):
        verify_g07_candidate_gate_pass(
            repository_root=root,
            development_checkout_root=development,
            expected_candidate_commit=commit,
            expected_candidate_tree=tree,
            component_receipt_paths=components,
            hardening_decision_path=drifted_decision,
            hardening_control_receipt_paths={},
            output_path=external / "drifted-pass.json",
        )


def test_g07_v3_binding_validates_current_governance_without_legacy_loader(
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
    ) = _candidate_repository(tmp_path, binding_version="current-goal-binding-v3")
    external = tmp_path / "external-v3"
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
            "threat_model_sha256": _threat_model_sha(root, commit),
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
        lambda *_args: ("refs/heads/codex/g07-candidate", commit, tree),
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

    assert receipt.remote_ref == "refs/heads/codex/g07-candidate"
    assert receipt.hardening_decision == "NOT_REQUIRED_WITH_RATIONALE"


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
            "threat_model_sha256": _threat_model_sha(root, commit),
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
            "threat_model_sha256": _threat_model_sha(root, commit),
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
    monkeypatch.setattr(
        build_agent_gate_pass,
        "validate_mainline_scope",
        lambda *_args, **_kwargs: SimpleNamespace(verdict="PASS", error_codes=[]),
    )
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
