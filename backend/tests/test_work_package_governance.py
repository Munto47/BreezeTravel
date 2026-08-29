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
    WORK_PACKAGE_PROTECTED_PATHS_V1,
    CurrentGoalBinding,
    HardeningDecisionReceipt,
    WorkPackageRegistry,
)
from evals.agent_gate_v1.work_packages import (
    WorkPackageValidationError,
    load_candidate_work_package_registry,
    load_work_package_registry,
    validate_package_checkout,
    validate_ready_to_merge_package,
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
    is_integrator = role == "INTEGRATOR"
    value: dict[str, object] = {
        "package_id": package_id,
        "goal_id": goal_id,
        "baseline_commit": BASELINE,
        "branch": branch,
        "remote_branch": f"origin/{branch}",
        "worktree_path": f"/worktrees/{package_id.lower()}",
        "role": role,
        "execution_mode": (
            "PRIMARY_INTEGRATOR_DIALOGUE"
            if is_integrator
            else "INDEPENDENT_FUNCTION_DIALOGUE"
        ),
        "dialogue_ref": (
            "codex-task:primary"
            if is_integrator
            else None
            if status == "WAITING_FOR_WRITER_SLOT"
            else f"codex-task:{package_id.lower()}"
        ),
        "dependencies": [],
        "owned_paths": [owned_path],
        "forbidden_paths": (
            [] if is_integrator else list(WORK_PACKAGE_PROTECTED_PATHS)
        ),
        "acceptance": ["targeted verification passes"],
        "merge_order": merge_order,
        "status": status,
    }
    if not is_integrator and goal_id == "TC-VNEXT-G02-MAP-STAY":
        value.update(
            {
                "prompt_path": (
                    "docs/governance/work-package-prompts/"
                    f"{package_id}.md"
                ),
                "prompt_sha256": "4" * 64,
                "registry_activation_commit": BASELINE,
            }
        )
    if status in {"READY_TO_MERGE", "MERGED"}:
        value["ready_commit"] = "5" * 40
    if status == "MERGED":
        value["merged_commit"] = "6" * 40
    return value


def _registry() -> dict[str, object]:
    return {
        "schema_version": "work-package-registry-v2",
        "program_id": "TC-VNEXT-2026",
        "active_goal_sequence": 2,
        "active_goal_id": "TC-VNEXT-G02-MAP-STAY",
        "mainline_phase": "CORE_MVP",
        "gate_profile": "CORE_AGENT_GATE",
        "guidance_sha256": "2" * 64,
        "mismatch_policy": "READ_ONLY",
        "max_parallel_writers": 3,
        "max_prepared_next_goal_packages": 2,
        "integration_order": [
            "WP-G02-STAY-DOMAIN",
            "WP-G02-MAP-STAY-BACKEND",
            "WP-G02-MAP-THEATER-UI",
        ],
        "e2e_after_all_merges": True,
        "packages": [
            _package(
                "WP-G02-INTEGRATOR",
                "codex/g02-integrator",
                "docs/governance",
                1,
                role="INTEGRATOR",
            ),
            _package(
                "WP-G02-MAP-THEATER-UI",
                "codex/wp-g02-map-theater-ui",
                "frontend/src/features/trip-check/map",
                30,
            ),
            _package(
                "WP-G02-STAY-DOMAIN",
                "codex/wp-g02-stay-domain",
                "backend/app/domain/stay",
                10,
            ),
            _package(
                "WP-G02-MAP-STAY-BACKEND",
                "codex/wp-g02-map-stay-backend",
                "backend/app/api/trip_check_map_stay.py",
                20,
                status="WAITING_FOR_WRITER_SLOT",
            ),
            _package(
                "WP-G03-EVIDENCE-PREP",
                "codex/g03-evidence-prep",
                "backend/app/domain/evidence",
                40,
                goal_id="TC-VNEXT-G03-TOP3-AUDIT",
                status="PREPARED_NOT_INTEGRATED",
            ),
        ],
    }


def test_valid_registry_allows_parallel_current_work_and_one_goal_ahead() -> None:
    registry = WorkPackageRegistry.model_validate(_registry())
    assert registry.mainline_phase == "CORE_MVP"
    assert len(registry.packages) == 5
    assert registry.packages[3].status == "WAITING_FOR_WRITER_SLOT"


def test_blocked_integrator_remains_a_valid_single_active_owner() -> None:
    value = deepcopy(_registry())
    packages = value["packages"]
    assert isinstance(packages, list)
    packages[0]["status"] = "BLOCKED_EXTERNAL"
    for item in packages[1:4]:
        item["status"] = "DEFERRED"
        item["dialogue_ref"] = item.get("dialogue_ref") or "codex-task:frozen"
    registry = WorkPackageRegistry.model_validate(value)
    assert registry.packages[0].status == "BLOCKED_EXTERNAL"


def test_integrator_plus_three_writable_contributors_is_rejected() -> None:
    value = deepcopy(_registry())
    packages = value["packages"]
    assert isinstance(packages, list)
    packages[3]["status"] = "IN_PROGRESS"
    packages[3]["dialogue_ref"] = "codex-task:backend"

    with pytest.raises(ValidationError, match="too many parallel writable"):
        WorkPackageRegistry.model_validate(value)


def test_protected_paths_cover_guidance_and_every_checked_in_lock_file() -> None:
    protected = set(WORK_PACKAGE_PROTECTED_PATHS)
    assert {"AGENTS.md", "CLAUDE.md"}.issubset(protected)
    tracked = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    lock_files = {
        path.replace("\\", "/")
        for path in tracked
        if Path(path).name.endswith(
            (".lock", "lock.json", "lock.yaml", "lock.yml")
        )
    }
    assert lock_files.issubset(protected)


def test_v1_registry_remains_parseable_but_has_no_v2_write_authority() -> None:
    value = deepcopy(_registry())
    value["schema_version"] = "work-package-registry-v1"
    value.pop("integration_order")
    value.pop("e2e_after_all_merges")
    for package in value["packages"]:
        for field in (
            "remote_branch",
            "worktree_path",
            "execution_mode",
            "dialogue_ref",
            "prompt_path",
            "prompt_sha256",
            "registry_activation_commit",
            "ready_commit",
            "merged_commit",
        ):
            package.pop(field, None)
        if package["role"] == "CONTRIBUTOR":
            package["forbidden_paths"] = [
                "AGENTS.md",
                "CLAUDE.md",
                "docs/governance/CURRENT_GOAL.md",
                "docs/governance/current_goal_binding.json",
                "docs/governance/current_work_packages.json",
                "backend/app/db/migrations",
                "packages/trip-check-client/openapi.json",
                "packages/trip-check-client/openapi.current.json",
                "packages/trip-check-client/src/generated",
                "frontend/package-lock.json",
                "miniapp/package-lock.json",
                "packages/trip-check-client/package-lock.json",
                "y-websocket/package-lock.json",
                "backend/eval_data/agent_gate_v1/automation_runner_requirements.lock",
                "backend/eval_data/agent_gate_v1/automation_runner_browser_package-lock.json",
            ]
    registry = WorkPackageRegistry.model_validate(value)
    assert registry.schema_version == "work-package-registry-v1"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("prompt_path", "complete prompt"),
        ("prompt_sha256", "complete prompt"),
        ("worktree_path", "complete prompt"),
        ("remote_branch", "complete prompt"),
        ("dialogue_ref", "dialogue reference"),
    ],
)
def test_started_contributor_requires_complete_dialogue_contract(
    field: str,
    message: str,
) -> None:
    value = deepcopy(_registry())
    package = value["packages"][1]
    package[field] = None
    with pytest.raises(ValidationError, match=message):
        WorkPackageRegistry.model_validate(value)


def test_subagent_cannot_be_registered_as_a_function_writer() -> None:
    value = deepcopy(_registry())
    value["packages"][1]["execution_mode"] = "PRIMARY_INTEGRATOR_DIALOGUE"
    with pytest.raises(ValidationError, match="user-visible dialogue mode"):
        WorkPackageRegistry.model_validate(value)


def test_ready_package_requires_frozen_commit() -> None:
    value = deepcopy(_registry())
    package = value["packages"][1]
    package["status"] = "READY_TO_MERGE"
    package.pop("ready_commit", None)
    with pytest.raises(ValidationError, match="ready_commit"):
        WorkPackageRegistry.model_validate(value)


def test_duplicate_branch_and_worktree_are_rejected() -> None:
    value = deepcopy(_registry())
    value["packages"][2]["branch"] = value["packages"][1]["branch"]
    value["packages"][2]["remote_branch"] = value["packages"][1]["remote_branch"]
    with pytest.raises(ValidationError, match="branches must be unique"):
        WorkPackageRegistry.model_validate(value)

    value = deepcopy(_registry())
    value["packages"][2]["worktree_path"] = value["packages"][1]["worktree_path"]
    with pytest.raises(ValidationError, match="worktrees must be unique"):
        WorkPackageRegistry.model_validate(value)


def test_g02_wrong_merge_order_is_rejected_before_gate() -> None:
    value = deepcopy(_registry())
    value["packages"][1]["merge_order"] = 5
    value["packages"][2]["merge_order"] = 30
    value["packages"][3]["merge_order"] = 20
    value["integration_order"] = [
        "WP-G02-MAP-THEATER-UI",
        "WP-G02-MAP-STAY-BACKEND",
        "WP-G02-STAY-DOMAIN",
    ]
    with pytest.raises(ValidationError, match="G02 integration order"):
        WorkPackageRegistry.model_validate(value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("second_integrator", "too many parallel"),
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
        packages.append(
            _package(
                "WP-G02-SECOND-INTEGRATOR",
                "codex/g02-second-integrator",
                "integration",
                50,
                role="INTEGRATOR",
            )
        )
    elif mutation == "three_next_prepared":
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
        packages[4]["goal_id"] = "TC-VNEXT-G04-SCREENSHOT"
    elif mutation == "overlap":
        packages[2]["owned_paths"] = [
            "frontend/src/features/trip-check/map/panel"
        ]
    elif mutation == "different_baseline":
        packages[2]["baseline_commit"] = "3" * 40
    elif mutation == "missing_protected":
        packages[1]["forbidden_paths"] = []
    elif mutation == "next_integrator":
        packages[4]["role"] = "INTEGRATOR"
        packages[4]["execution_mode"] = "PRIMARY_INTEGRATOR_DIALOGUE"
        packages[4]["forbidden_paths"] = []
    elif mutation == "current_depends_next":
        packages[4]["merge_order"] = 5
        packages[1]["dependencies"] = [packages[4]["package_id"]]
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


def _prompt_bytes(package: dict[str, object]) -> bytes:
    paths = [*package["owned_paths"], *package["forbidden_paths"]]
    path_lines = "\n".join(f"- {path}" for path in paths)
    return (
        "# Work Package Prompt v1\n\n"
        "## Identity and exact baseline\n\n"
        "prompt_schema_version: work-package-prompt-v1\n"
        f"package_id: {package['package_id']}\n"
        f"goal_id: {package['goal_id']}\n"
        f"role: {package['role']}\n"
        f"baseline_commit: {package['baseline_commit']}\n"
        f"registry_activation_commit: {package['registry_activation_commit']}\n\n"
        "## Branch and isolated worktree\n\n"
        f"branch: {package['branch']}\n"
        f"remote_branch: {package['remote_branch']}\n"
        f"worktree_path: {package['worktree_path']}\n\n"
        "## Owned and forbidden paths\n\n"
        f"{path_lines}\n\n"
        "## User-observable outcome\n\nDeliver the registered slice.\n\n"
        "## Non-goals and locked contracts\n\nDo not change shared contracts.\n\n"
        "## Dependencies, inputs and outputs\n\nUse only registered dependencies.\n\n"
        "## Acceptance and targeted verification\n\nRun targeted tests.\n\n"
        "## Git and authority restrictions\n\n"
        "must_not_merge: true\n"
        "must_not_modify_goal_or_registry: true\n"
        "Only commit and push this package; include remote readback.\n\n"
        "## Subagent boundary\n\nsubagent_read_only: true\n\n"
        "## Completion report\n\n"
        "Return READY_TO_MERGE, IN_PROGRESS, or BLOCKED_EXTERNAL.\n"
    ).encode("utf-8")


def _git_governance_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repository"
    feature_root = tmp_path / "feature-worktree"
    remote = tmp_path / "remote.git"
    (root / "docs/governance/work-package-prompts").mkdir(parents=True)
    (root / "feature").mkdir()
    _run_git(tmp_path, "init", "--bare", str(remote))
    _run_git(root.parent, "init", str(root))
    _run_git(root, "config", "user.email", "gate@example.test")
    _run_git(root, "config", "user.name", "Gate Test")
    _run_git(root, "config", "core.autocrlf", "false")
    _run_git(root, "checkout", "-b", "codex/integrator")
    _run_git(root, "remote", "add", "origin", str(remote))
    agents = b"# authoritative guidance\n"
    (root / "AGENTS.md").write_bytes(agents)
    (root / "CLAUDE.md").write_text("# mirror\n", encoding="utf-8")
    (root / "feature/value.txt").write_text("base\n", encoding="utf-8")
    baseline = _commit_all(root, "base")
    contributor = {
        "package_id": "WP-G01-TEST-PACKAGE",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "baseline_commit": baseline,
        "branch": "codex/test-package",
        "remote_branch": "origin/codex/test-package",
        "worktree_path": feature_root.resolve().as_posix(),
        "role": "CONTRIBUTOR",
        "execution_mode": "INDEPENDENT_FUNCTION_DIALOGUE",
        "dialogue_ref": "codex-task:test-package",
        "prompt_path": (
            "docs/governance/work-package-prompts/WP-G01-TEST-PACKAGE.md"
        ),
        "prompt_sha256": "0" * 64,
        "registry_activation_commit": baseline,
        "dependencies": [],
        "owned_paths": ["feature"],
        "forbidden_paths": list(WORK_PACKAGE_PROTECTED_PATHS),
        "acceptance": ["targeted test succeeds"],
        "merge_order": 10,
        "status": "IN_PROGRESS",
    }
    prompt = _prompt_bytes(contributor)
    contributor["prompt_sha256"] = hashlib.sha256(prompt).hexdigest()
    (root / contributor["prompt_path"]).write_bytes(prompt)
    registry = {
        "schema_version": "work-package-registry-v2",
        "active_goal_sequence": 1,
        "active_goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "mainline_phase": "CORE_MVP",
        "gate_profile": "CORE_AGENT_GATE",
        "guidance_sha256": hashlib.sha256(agents).hexdigest(),
        "integration_order": ["WP-G01-TEST-PACKAGE"],
        "e2e_after_all_merges": True,
        "packages": [
            {
                "package_id": "WP-G01-INTEGRATOR",
                "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
                "baseline_commit": baseline,
                "branch": "codex/integrator",
                "remote_branch": "origin/codex/integrator",
                "worktree_path": root.resolve().as_posix(),
                "role": "INTEGRATOR",
                "execution_mode": "PRIMARY_INTEGRATOR_DIALOGUE",
                "dialogue_ref": "codex-task:primary",
                "dependencies": [],
                "owned_paths": ["docs/governance"],
                "forbidden_paths": [],
                "acceptance": ["integration succeeds"],
                "merge_order": 1,
                "status": "IN_PROGRESS",
            },
            contributor,
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
    _run_git(
        root,
        "worktree",
        "add",
        "-b",
        "codex/test-package",
        str(feature_root),
        activation,
    )
    _run_git(root, "push", "-u", "origin", "codex/integrator")
    _run_git(feature_root, "push", "-u", "origin", "codex/test-package")
    return root, feature_root, activation


def test_git_validator_allows_owned_change_and_rejects_forbidden_change(
    tmp_path: Path,
) -> None:
    _root, feature_root, _activation = _git_governance_repo(tmp_path)
    (feature_root / "feature/value.txt").write_text("changed\n", encoding="utf-8")
    assert validate_package_checkout(
        feature_root,
        "WP-G01-TEST-PACKAGE",
    ).package_id == (
        "WP-G01-TEST-PACKAGE"
    )
    (feature_root / "CLAUDE.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(WorkPackageValidationError, match="outside package ownership"):
        validate_package_checkout(feature_root, "WP-G01-TEST-PACKAGE")


def test_integrator_can_only_accept_paths_from_merged_contributors(
    tmp_path: Path,
) -> None:
    root, feature_root, _activation = _git_governance_repo(tmp_path)
    (feature_root / "feature/value.txt").write_text("ready\n", encoding="utf-8")
    ready_commit = _commit_all(feature_root, "finish contributor")
    _run_git(feature_root, "push", "origin", "codex/test-package")
    _run_git(root, "merge", "--no-ff", "codex/test-package", "-m", "merge package")
    merged_commit = _run_git(root, "rev-parse", "HEAD")
    registry_path = root / "docs/governance/current_work_packages.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["packages"][1]["status"] = "MERGED"
    registry["packages"][1]["ready_commit"] = ready_commit
    registry["packages"][1]["merged_commit"] = merged_commit
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
    root, _feature_root, activation = _git_governance_repo(tmp_path)
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
    registry_path = root / "docs/governance/current_work_packages.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["schema_version"] = "work-package-registry-v1"
    registry["packages"][1]["forbidden_paths"] = list(
        WORK_PACKAGE_PROTECTED_PATHS_V1
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    v1_commit = _commit_all(root, "downgrade registry")
    with pytest.raises(WorkPackageValidationError, match="registry must be v2"):
        load_candidate_work_package_registry(
            root,
            v1_commit,
            binding,
            require_gate_ready=False,
        )
    registry["schema_version"] = "work-package-registry-v2"
    registry["packages"][1]["forbidden_paths"] = list(
        WORK_PACKAGE_PROTECTED_PATHS
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    _commit_all(root, "restore registry v2")
    (root / "AGENTS.md").write_text("changed guidance\n", encoding="utf-8")
    bad_commit = _commit_all(root, "change guidance")
    with pytest.raises(WorkPackageValidationError, match="registered guidance"):
        load_candidate_work_package_registry(
            root, bad_commit, binding, require_gate_ready=False
        )


def test_prompt_hash_and_complete_contract_are_required_to_start(
    tmp_path: Path,
) -> None:
    root, _feature_root, _activation = _git_governance_repo(tmp_path)
    registry_path = root / "docs/governance/current_work_packages.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    package = registry["packages"][1]
    prompt_path = root / package["prompt_path"]
    original = prompt_path.read_bytes()
    prompt_path.write_bytes(original + b"\nchanged\n")
    with pytest.raises(WorkPackageValidationError, match="prompt hash differs"):
        load_work_package_registry(root)

    incomplete = original.replace(b"## Completion report\n", b"")
    prompt_path.write_bytes(incomplete)
    package["prompt_sha256"] = hashlib.sha256(incomplete).hexdigest()
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(WorkPackageValidationError, match="prompt is incomplete"):
        load_work_package_registry(root)


def test_ready_freeze_rejects_dirty_or_advanced_function_branch(
    tmp_path: Path,
) -> None:
    root, feature_root, _activation = _git_governance_repo(tmp_path)
    (feature_root / "feature/value.txt").write_text("ready\n", encoding="utf-8")
    ready_commit = _commit_all(feature_root, "ready package")
    _run_git(feature_root, "push", "origin", "codex/test-package")

    registry_path = root / "docs/governance/current_work_packages.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["packages"][1]["status"] = "READY_TO_MERGE"
    registry["packages"][1]["ready_commit"] = ready_commit
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    _commit_all(root, "freeze ready package")
    assert validate_ready_to_merge_package(
        root,
        "WP-G01-TEST-PACKAGE",
    ).ready_commit == ready_commit

    dirty_path = feature_root / "feature/untracked.txt"
    dirty_path.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorkPackageValidationError, match="became dirty"):
        validate_ready_to_merge_package(root, "WP-G01-TEST-PACKAGE")
    dirty_path.unlink()

    (feature_root / "feature/value.txt").write_text("advanced\n", encoding="utf-8")
    _commit_all(feature_root, "illegal post-freeze commit")
    with pytest.raises(WorkPackageValidationError, match="tip changed"):
        validate_ready_to_merge_package(root, "WP-G01-TEST-PACKAGE")


def test_candidate_gate_rechecks_ready_tip_and_registered_merge_history(
    tmp_path: Path,
) -> None:
    root, feature_root, _activation = _git_governance_repo(tmp_path)
    (feature_root / "feature/value.txt").write_text("ready\n", encoding="utf-8")
    ready_commit = _commit_all(feature_root, "ready package")
    _run_git(feature_root, "push", "origin", "codex/test-package")
    _run_git(root, "merge", "--no-ff", "codex/test-package", "-m", "merge package")
    merged_commit = _run_git(root, "rev-parse", "HEAD")

    registry_path = root / "docs/governance/current_work_packages.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    package = registry["packages"][1]
    package["status"] = "MERGED"
    package["ready_commit"] = ready_commit
    package["merged_commit"] = merged_commit
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    candidate_commit = _commit_all(root, "record serial integration")
    binding = CurrentGoalBinding.model_validate_json(
        (root / "docs/governance/current_goal_binding.json").read_bytes()
    )
    loaded, _registry_sha = load_candidate_work_package_registry(
        root,
        candidate_commit,
        binding,
        require_gate_ready=True,
    )
    assert loaded.packages[1].merged_commit == merged_commit

    (feature_root / "feature/value.txt").write_text("illegal drift\n", encoding="utf-8")
    _commit_all(feature_root, "advance after freeze")
    _run_git(feature_root, "push", "origin", "codex/test-package")
    with pytest.raises(WorkPackageValidationError, match="advanced after READY"):
        load_candidate_work_package_registry(
            root,
            candidate_commit,
            binding,
            require_gate_ready=True,
        )
