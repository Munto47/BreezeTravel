from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from evals.agent_gate_v1.contracts import (
    WORK_PACKAGE_PROTECTED_PATHS,
    CurrentGoalBinding,
    HardeningDecisionReceipt,
    WorkPackageRegistry,
)
from evals.agent_gate_v1.work_packages import (
    WorkPackageValidationError,
    load_candidate_work_package_registry,
    validate_package_checkout,
)


BASELINE = "1" * 40
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _package(
    package_id: str,
    branch: str,
    owned_path: str,
    merge_order: int,
    *,
    goal_id: str = "TC-VNEXT-G02-MAP-STAY",
    role: str = "CONTRIBUTOR",
    status: str = "IN_PROGRESS",
) -> dict[str, object]:
    return {
        "package_id": package_id,
        "goal_id": goal_id,
        "baseline_commit": BASELINE,
        "branch": branch,
        "role": role,
        "dependencies": [],
        "owned_paths": [owned_path],
        "forbidden_paths": (
            [] if role == "INTEGRATOR" else list(WORK_PACKAGE_PROTECTED_PATHS)
        ),
        "acceptance": ["targeted verification passes"],
        "merge_order": merge_order,
        "status": status,
    }


def _registry() -> dict[str, object]:
    return {
        "schema_version": "work-package-registry-v1",
        "program_id": "TC-VNEXT-2026",
        "active_goal_sequence": 2,
        "active_goal_id": "TC-VNEXT-G02-MAP-STAY",
        "mainline_phase": "CORE_MVP",
        "gate_profile": "CORE_AGENT_GATE",
        "guidance_sha256": "2" * 64,
        "mismatch_policy": "READ_ONLY",
        "max_parallel_writers": 3,
        "max_prepared_next_goal_packages": 2,
        "packages": [
            _package(
                "WP-G02-INTEGRATOR",
                "codex/g02-integrator",
                "docs/governance",
                1,
                role="INTEGRATOR",
            ),
            _package(
                "WP-G02-MAP-UI",
                "codex/g02-map-ui",
                "frontend/src/features/map",
                2,
            ),
            _package(
                "WP-G02-STAY-DOMAIN",
                "codex/g02-stay-domain",
                "backend/app/domain/stay",
                3,
                status="READY_TO_MERGE",
            ),
            _package(
                "WP-G03-EVIDENCE-PREP",
                "codex/g03-evidence-prep",
                "backend/app/domain/evidence",
                4,
                goal_id="TC-VNEXT-G03-TOP3-AUDIT",
                status="PREPARED_NOT_INTEGRATED",
            ),
        ],
    }


def test_valid_registry_allows_parallel_current_work_and_one_goal_ahead() -> None:
    registry = WorkPackageRegistry.model_validate(_registry())
    assert registry.mainline_phase == "CORE_MVP"
    assert len(registry.packages) == 4


def test_blocked_integrator_remains_a_valid_single_active_owner() -> None:
    value = deepcopy(_registry())
    packages = value["packages"]
    assert isinstance(packages, list)
    packages[0]["status"] = "BLOCKED_EXTERNAL"
    for item in packages[1:]:
        item["status"] = "DEFERRED"
    registry = WorkPackageRegistry.model_validate(value)
    assert registry.packages[0].status == "BLOCKED_EXTERNAL"


def test_integrator_plus_three_writable_contributors_is_rejected() -> None:
    value = deepcopy(_registry())
    packages = value["packages"]
    assert isinstance(packages, list)
    packages[2]["status"] = "IN_PROGRESS"

    with pytest.raises(ValidationError, match="too many parallel writable"):
        WorkPackageRegistry.model_validate(value)


def test_protected_paths_cover_guidance_and_every_checked_in_lock_file() -> None:
    protected = set(WORK_PACKAGE_PROTECTED_PATHS)
    assert {"AGENTS.md", "CLAUDE.md"}.issubset(protected)
    lock_files = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for pattern in ("*.lock", "*lock.json", "*lock.yaml", "*lock.yml")
        for path in REPOSITORY_ROOT.rglob(pattern)
        if ".git" not in path.parts
    }
    assert lock_files.issubset(protected)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("second_integrator", "exactly one active"),
        ("four_contributors", "too many parallel"),
        ("three_next_prepared", "too many next Goal"),
        ("skip_two_goals", "active or next Goal"),
        ("overlap", "owned paths overlap"),
        ("different_baseline", "one exact baseline"),
        ("missing_protected", "forbid every integrator-owned"),
        ("next_integrator", "next Goal packages must be contributors"),
        ("current_depends_next", "current Goal cannot depend on the next Goal"),
    ],
)
def test_invalid_parallel_topologies_are_rejected(
    mutation: str,
    message: str,
) -> None:
    value = deepcopy(_registry())
    packages = value["packages"]
    assert isinstance(packages, list)
    if mutation == "second_integrator":
        packages[1]["role"] = "INTEGRATOR"
        packages[1]["forbidden_paths"] = []
    elif mutation == "four_contributors":
        packages.extend(
            [
                _package(
                    "WP-G02-API-A",
                    "codex/g02-api-a",
                    "backend/app/api/a",
                    5,
                ),
                _package(
                    "WP-G02-API-B",
                    "codex/g02-api-b",
                    "backend/app/api/b",
                    6,
                ),
            ]
        )
    elif mutation == "three_next_prepared":
        del packages[1:3]
        packages.extend(
            [
                _package(
                    "WP-G03-AUDIT-PREP",
                    "codex/g03-audit-prep",
                    "backend/app/domain/audit",
                    5,
                    goal_id="TC-VNEXT-G03-TOP3-AUDIT",
                    status="PREPARED_NOT_INTEGRATED",
                ),
                _package(
                    "WP-G03-UI-PREP",
                    "codex/g03-ui-prep",
                    "frontend/src/features/audit",
                    6,
                    goal_id="TC-VNEXT-G03-TOP3-AUDIT",
                    status="PREPARED_NOT_INTEGRATED",
                ),
            ]
        )
    elif mutation == "skip_two_goals":
        packages[3]["goal_id"] = "TC-VNEXT-G04-SCREENSHOT"
    elif mutation == "overlap":
        packages[2]["owned_paths"] = ["frontend/src/features/map/panel"]
    elif mutation == "different_baseline":
        packages[2]["baseline_commit"] = "3" * 40
    elif mutation == "missing_protected":
        packages[1]["forbidden_paths"] = []
    elif mutation == "next_integrator":
        packages[3]["role"] = "INTEGRATOR"
        packages[3]["forbidden_paths"] = []
    elif mutation == "current_depends_next":
        packages[1]["dependencies"] = [packages[3]["package_id"]]
        packages[1]["merge_order"] = 5
    with pytest.raises(ValidationError, match=message):
        WorkPackageRegistry.model_validate(value)


def test_v1_current_binding_remains_readable_but_v2_requires_phase_and_registry() -> None:
    value = {
        "schema_version": "current-goal-binding-v1",
        "goal_sequence": 4,
        "goal_id": "TC-VNEXT-G04-SCREENSHOT",
        "status": "APPROVED",
        "predecessor_goal_id": "TC-VNEXT-G03-TOP3-AUDIT",
        "predecessor_completion_commit": "4" * 40,
        "automated_gate_contract_path": "backend/eval_data/agent_gate_v1/g04.json",
        "automated_gate_contract_sha256": "5" * 64,
        "gate_profile": "CORE_AGENT_GATE",
    }
    historical = CurrentGoalBinding.model_validate(value)
    assert historical.mainline_phase == "PRODUCT_ENHANCEMENT"

    value["schema_version"] = "current-goal-binding-v2"
    with pytest.raises(ValidationError, match="wrong mainline phase"):
        CurrentGoalBinding.model_validate(value)


def test_g07_hardening_decision_does_not_enable_legacy_controls_by_default() -> None:
    value = {
        "candidate_commit": "6" * 40,
        "candidate_tree": "7" * 40,
        "threat_model_sha256": "8" * 64,
        "decision": "NOT_REQUIRED_WITH_RATIONALE",
        "identified_threats": ["candidate evidence could be changed accidentally"],
        "selected_controls": [],
        "alternative_controls": ["clean checkout and remote subject/tree readback"],
        "residual_risks": ["process isolation is not organizational independence"],
        "rationale": "The current candidate threat model does not justify external custody.",
    }
    receipt = HardeningDecisionReceipt.model_validate(value)
    assert receipt.selected_controls == []

    value["selected_controls"] = ["ISOLATED_OCI"]
    with pytest.raises(ValidationError, match="cannot enable legacy controls"):
        HardeningDecisionReceipt.model_validate(value)


def test_generated_json_schemas_enforce_v2_binding_and_core_isolation() -> None:
    root = Path(__file__).resolve().parents[1] / "eval_data" / "agent_gate_v1"
    binding_schema = json.loads(
        (root / "current_goal_binding.schema.json").read_text(encoding="utf-8")
    )
    binding = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/governance/current_goal_binding.json"
        ).read_text(encoding="utf-8")
    )
    binding.pop("mainline_phase")
    binding.pop("work_package_registry_path")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(binding, binding_schema)

    automation_schema = json.loads(
        (root / "automated_product_gate_contract.schema.json").read_text(
            encoding="utf-8"
        )
    )
    core_contract = json.loads(
        (root / "g01_automated_product_gate.json").read_text(encoding="utf-8")
    )
    core_contract["isolation"] = {
        "mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
        "runner_recipe_path": "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile",
        "runner_recipe_sha256": "1" * 64,
        "runner_entrypoint_path": "backend/eval_data/agent_gate_v1/automation_runner_entrypoint.sh",
        "runner_entrypoint_sha256": "2" * 64,
        "runner_context_policy_path": "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile.dockerignore",
        "runner_context_policy_sha256": "3" * 64,
        "network_access": False,
        "host_mount_count": 0,
        "host_pid_namespace": False,
        "synthetic_profile": True,
        "authority_secret_mount_count": 0,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(core_contract, automation_schema)


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(root: Path, message: str) -> str:
    _run_git(root, "add", "--all")
    _run_git(root, "commit", "-m", message)
    return _run_git(root, "rev-parse", "HEAD")


def _git_governance_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    (root / "docs/governance").mkdir(parents=True)
    (root / "feature").mkdir()
    _run_git(root.parent, "init", str(root))
    _run_git(root, "config", "user.email", "gate@example.test")
    _run_git(root, "config", "user.name", "Gate Test")
    _run_git(root, "checkout", "-b", "codex/test-package")
    agents = b"# authoritative guidance\n"
    (root / "AGENTS.md").write_bytes(agents)
    (root / "CLAUDE.md").write_text("# mirror\n", encoding="utf-8")
    (root / "feature/value.txt").write_text("base\n", encoding="utf-8")
    baseline = _commit_all(root, "base")
    registry = {
        "active_goal_sequence": 1,
        "active_goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "mainline_phase": "CORE_MVP",
        "gate_profile": "CORE_AGENT_GATE",
        "guidance_sha256": hashlib.sha256(agents).hexdigest(),
        "packages": [
            {
                "package_id": "WP-G01-INTEGRATOR",
                "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
                "baseline_commit": baseline,
                "branch": "codex/integrator",
                "role": "INTEGRATOR",
                "dependencies": [],
                "owned_paths": ["docs/governance"],
                "forbidden_paths": [],
                "acceptance": ["integration succeeds"],
                "merge_order": 1,
                "status": "IN_PROGRESS",
            },
            {
                "package_id": "WP-G01-TEST-PACKAGE",
                "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
                "baseline_commit": baseline,
                "branch": "codex/test-package",
                "role": "CONTRIBUTOR",
                "dependencies": [],
                "owned_paths": ["feature"],
                "forbidden_paths": list(WORK_PACKAGE_PROTECTED_PATHS),
                "acceptance": ["targeted test succeeds"],
                "merge_order": 2,
                "status": "IN_PROGRESS",
            },
        ],
    }
    binding = {
        "schema_version": "current-goal-binding-v2",
        "goal_sequence": 1,
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "status": "IN_PROGRESS",
        "predecessor_goal_id": "TC-BP-G00-BLUEPRINT",
        "predecessor_completion_commit": "1" * 40,
        "automated_gate_contract_path": "gate.json",
        "automated_gate_contract_sha256": "2" * 64,
        "gate_profile": "CORE_AGENT_GATE",
        "mainline_phase": "CORE_MVP",
        "work_package_registry_path": "docs/governance/current_work_packages.json",
    }
    (root / "docs/governance/current_work_packages.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    (root / "docs/governance/current_goal_binding.json").write_text(
        json.dumps(binding), encoding="utf-8"
    )
    activation = _commit_all(root, "activate registry")
    return root, activation


def test_git_validator_allows_owned_change_and_rejects_forbidden_change(
    tmp_path: Path,
) -> None:
    root, _activation = _git_governance_repo(tmp_path)
    (root / "feature/value.txt").write_text("changed\n", encoding="utf-8")
    assert validate_package_checkout(root, "WP-G01-TEST-PACKAGE").package_id == (
        "WP-G01-TEST-PACKAGE"
    )
    (root / "CLAUDE.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(WorkPackageValidationError, match="outside package ownership"):
        validate_package_checkout(root, "WP-G01-TEST-PACKAGE")


def test_integrator_can_only_accept_paths_from_merged_contributors(
    tmp_path: Path,
) -> None:
    root, _activation = _git_governance_repo(tmp_path)
    _run_git(root, "checkout", "-b", "codex/integrator")
    registry_path = root / "docs/governance/current_work_packages.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["packages"][1]["status"] = "MERGED"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    _commit_all(root, "record contributor merge")

    (root / "feature/value.txt").write_text("integrated\n", encoding="utf-8")
    assert validate_package_checkout(root, "WP-G01-INTEGRATOR").package_id == (
        "WP-G01-INTEGRATOR"
    )
    (root / "outside.txt").write_text("not registered\n", encoding="utf-8")
    with pytest.raises(WorkPackageValidationError, match="outside package ownership"):
        validate_package_checkout(root, "WP-G01-INTEGRATOR")


def test_candidate_registry_rejects_v1_and_guidance_mismatch(tmp_path: Path) -> None:
    root, activation = _git_governance_repo(tmp_path)
    binding_path = root / "docs/governance/current_goal_binding.json"
    binding = CurrentGoalBinding.model_validate_json(binding_path.read_bytes())
    _, registry_sha = load_candidate_work_package_registry(
        root, activation, binding, require_gate_ready=False
    )
    assert len(registry_sha) == 64
    historical = binding.model_copy(update={"schema_version": "current-goal-binding-v1"})
    with pytest.raises(WorkPackageValidationError, match="must be v2"):
        load_candidate_work_package_registry(
            root, activation, historical, require_gate_ready=False
        )
    (root / "AGENTS.md").write_text("changed guidance\n", encoding="utf-8")
    bad_commit = _commit_all(root, "change guidance")
    with pytest.raises(WorkPackageValidationError, match="registered guidance"):
        load_candidate_work_package_registry(
            root, bad_commit, binding, require_gate_ready=False
        )
