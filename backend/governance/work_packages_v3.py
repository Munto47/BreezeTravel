from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


REGISTRY_PATH = Path("docs/governance/current_work_packages.json")
BINDING_PATH = Path("docs/governance/current_goal_binding.json")
CURRENT_GOAL_PATH = Path("docs/governance/CURRENT_GOAL.md")
GUIDANCE_PATH = Path("AGENTS.md")
POLICY_PATH = Path("docs/governance/product_delivery_gates.json")
FROZEN_AGENT_GATE_PREFIX = "backend/evals/agent_gate_v1/"
G07_GOAL_ID = "TC-VNEXT-G07-CANDIDATE"
PACKAGE_STATUSES = {
    "PREPARED_NOT_INTEGRATED",
    "WAITING_FOR_WRITER_SLOT",
    "IN_PROGRESS",
    "READY_TO_MERGE",
    "MERGED",
    "DEFERRED",
    "BLOCKED_EXTERNAL",
}
ACTIVE_CONTRIBUTOR_STATUSES = {"IN_PROGRESS", "BLOCKED_EXTERNAL"}
DEVELOPMENT_PHASES = {"IMPLEMENTING", "REPAIR_ACTIVE"}
FINAL_EVIDENCE_PHASES = {
    "EVIDENCE_FROZEN",
    "GATE_RUNNING",
    "DELIVERY_PASS_RECORDED",
    "TRANSITION_READY",
}
LEGAL_PHASES = DEVELOPMENT_PHASES | FINAL_EVIDENCE_PHASES | {
    "PREFLIGHT",
    "DELIVERY_VERIFY",
    "GOAL_TRANSITION",
}
OWNER_REVIEW_PROGRAM_STATE = "CORE_MVP_OWNER_REVIEW_PENDING"
REGISTRY_BINDING_MUTABLE_FIELDS = {
    "status",
    "registry_binding_commit",
    "branch_point_commit",
    "ready_commit",
    "merged_commit",
    "remote_readback_commit",
}
G07_MUTABLE_AGENT_GATE_PATHS = {
    "backend/evals/agent_gate_v1/candidate_component_verifiers.py",
    "backend/evals/agent_gate_v1/candidate_gate.py",
    "backend/evals/agent_gate_v1/contracts.py",
}
G04_GOAL_ID = "TC-VNEXT-G04-SCREENSHOT"
G04_FORMAL_RECEIPT_PATH = "backend/governance/g04_screenshot_parity_receipt.json"
G04_FORMAL_VALIDATOR_PATH = "backend/governance/g04_screenshot_parity.py"
G04_PRODUCT_RECEIPT_PATH = "docs/governance/gate-results/G04.product-delivery.json"
_GOAL_STATE = re.compile(
    r"<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE\n(?P<payload>\{.*?\})\n-->",
    re.DOTALL,
)


class WorkPackageV3Error(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkPackageV3Error(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise WorkPackageV3Error(f"JSON root must be an object: {path}")
    return value


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise WorkPackageV3Error(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _git_succeeds(root: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def _normalized_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").rstrip("/").casefold()


def _git_blob_bytes(root: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise WorkPackageV3Error(f"cannot read {path} from {commit}")
    return result.stdout


def _remote_branch_tip(root: Path, branch: str) -> str | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2:
        return None
    commit, ref = lines[0]
    if ref != f"refs/heads/{branch}" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    return commit


def _registry_binding_snapshot_errors(
    root: Path,
    package: dict[str, Any],
) -> tuple[str, ...]:
    package_name = str(package.get("package_id"))
    binding_commit = package.get("registry_binding_commit")
    if not isinstance(binding_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", binding_commit
    ):
        return (f"{package_name}:REGISTRY_BINDING_COMMIT_INVALID",)
    try:
        snapshot_registry = json.loads(
            _git_blob_bytes(root, binding_commit, REGISTRY_PATH.as_posix()).decode(
                "utf-8"
            )
        )
    except (UnicodeDecodeError, json.JSONDecodeError, WorkPackageV3Error):
        return (f"{package_name}:REGISTRY_BINDING_SNAPSHOT_UNREADABLE",)
    snapshot_packages = snapshot_registry.get("packages")
    if not isinstance(snapshot_packages, list):
        return (f"{package_name}:REGISTRY_BINDING_PACKAGE_MISSING",)
    snapshot_package = next(
        (
            item
            for item in snapshot_packages
            if isinstance(item, dict) and item.get("package_id") == package_name
        ),
        None,
    )
    if snapshot_package is None:
        return (f"{package_name}:REGISTRY_BINDING_PACKAGE_MISSING",)
    errors: list[str] = []
    if snapshot_package.get("status") != "WAITING_FOR_WRITER_SLOT":
        errors.append(f"{package_name}:REGISTRY_BINDING_STATUS_INVALID")
    current_stable = {
        key: value
        for key, value in package.items()
        if key not in REGISTRY_BINDING_MUTABLE_FIELDS
    }
    snapshot_stable = {
        key: value
        for key, value in snapshot_package.items()
        if key not in REGISTRY_BINDING_MUTABLE_FIELDS
    }
    if current_stable != snapshot_stable:
        errors.append(f"{package_name}:REGISTRY_BINDING_SNAPSHOT_MISMATCH")
    prompt_path = package.get("prompt_path")
    prompt_hash = package.get("prompt_sha256")
    if not isinstance(prompt_path, str) or not isinstance(prompt_hash, str):
        errors.append(f"{package_name}:REGISTRY_BINDING_PROMPT_INVALID")
    else:
        try:
            blob_hash = hashlib.sha256(
                _git_blob_bytes(root, binding_commit, prompt_path)
            ).hexdigest()
        except WorkPackageV3Error:
            errors.append(f"{package_name}:REGISTRY_BINDING_PROMPT_UNREADABLE")
        else:
            if blob_hash != prompt_hash:
                errors.append(f"{package_name}:REGISTRY_BINDING_PROMPT_MISMATCH")
    return tuple(errors)


def _commit_paths(root: Path, commit: str) -> tuple[str, ...]:
    value = _git(
        root,
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-only",
        "-r",
        commit,
    )
    return tuple(sorted(item for item in value.splitlines() if item))


def _stable_patch_id(root: Path, commit: str) -> str:
    shown = subprocess.run(
        ["git", "-C", str(root), "show", "--pretty=format:", "--binary", commit],
        check=False,
        capture_output=True,
    )
    if shown.returncode != 0:
        raise WorkPackageV3Error(f"cannot read commit for patch-id: {commit}")
    patched = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=shown.stdout,
        check=False,
        capture_output=True,
    )
    if patched.returncode != 0 or not patched.stdout.strip():
        raise WorkPackageV3Error(f"cannot calculate patch-id: {commit}")
    return patched.stdout.decode("ascii", errors="replace").split()[0]


def _is_within(path: str, roots: list[str]) -> bool:
    return any(path == root.rstrip("/") or path.startswith(f"{root.rstrip('/')}/") for root in roots)


def _agent_gate_path_is_authorized_for_g07(
    path: str,
    *,
    registry: dict[str, Any],
    active_slice: dict[str, Any],
) -> bool:
    return (
        registry.get("active_goal_id") == G07_GOAL_ID
        and active_slice.get("work_kind") == "CANDIDATE_HARDENING"
        and active_slice.get("slice_id")
        in {
            "G07-CANDIDATE-CONTRACT",
            "G07-COMPONENT-RAW-REVALIDATION",
            "G07-SEALED-ONE-SHOT",
        }
        and path in G07_MUTABLE_AGENT_GATE_PATHS
    )


def _working_paths(root: Path, base_commit: str) -> tuple[str, ...]:
    values: set[str] = set()
    for args in (
        ("diff", "--name-only", f"{base_commit}..HEAD"),
        ("diff", "--name-only"),
        ("diff", "--cached", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        values.update(item for item in _git(root, *args).splitlines() if item)
    return tuple(sorted(values))


def _goal_state(root: Path) -> dict[str, Any]:
    text = (root / CURRENT_GOAL_PATH).read_text(encoding="utf-8")
    match = _GOAL_STATE.search(text)
    if match is None:
        raise WorkPackageV3Error("CURRENT_GOAL machine state is missing")
    value = json.loads(match.group("payload"))
    if not isinstance(value, dict):
        raise WorkPackageV3Error("CURRENT_GOAL machine state is invalid")
    return value


def validate_registry_v3(
    repository_root: Path,
    *,
    check_scope: bool = False,
    package_id: str | None = None,
    head_ref: str = "HEAD",
    require_all_merged: bool = False,
) -> dict[str, Any]:
    root = repository_root.resolve()
    registry = _read_json(root / REGISTRY_PATH)
    if registry.get("schema_version") != "work-package-registry-v3":
        raise WorkPackageV3Error("registry is not work-package-registry-v3")
    binding = _read_json(root / BINDING_PATH)
    goal_state = _goal_state(root)
    errors: list[str] = []

    if registry.get("active_goal_id") != binding.get("goal_id"):
        errors.append("GOAL_BINDING_MISMATCH")
    if registry.get("active_goal_sequence") != binding.get("goal_sequence"):
        errors.append("GOAL_SEQUENCE_MISMATCH")
    if registry.get("program_state") != binding.get("program_state"):
        errors.append("PROGRAM_STATE_MISMATCH")
    if goal_state.get("goal_id") != binding.get("goal_id"):
        errors.append("CURRENT_GOAL_ID_MISMATCH")
    if goal_state.get("goal_status") != binding.get("status"):
        errors.append("CURRENT_GOAL_STATUS_MISMATCH")
    if not (
        registry.get("next_goal_id")
        == binding.get("next_goal_id")
        == goal_state.get("next_goal_id")
    ):
        errors.append("NEXT_GOAL_BINDING_MISMATCH")
    if registry.get("guidance_sha256") != _sha256(root / GUIDANCE_PATH):
        errors.append("GUIDANCE_HASH_MISMATCH")
    if registry.get("scope_policy_sha256") != _sha256(root / POLICY_PATH):
        errors.append("SCOPE_POLICY_HASH_MISMATCH")
    if registry.get("max_parallel_writers") != 2:
        errors.append("PARALLEL_WRITER_LIMIT_INVALID")

    active_slice = registry.get("active_slice")

    if registry.get("active_goal_id") == G04_GOAL_ID:
        lifecycle = registry.get("delivery_evidence")
        if not isinstance(lifecycle, dict):
            errors.append("G04_DELIVERY_EVIDENCE_LIFECYCLE_MISSING")
        else:
            expected_lifecycle_keys = {
                "state",
                "fixture_ci",
                "formal_parity",
                "transition_requires_formal_pass",
            }
            if set(lifecycle) != expected_lifecycle_keys:
                errors.append("G04_DELIVERY_EVIDENCE_LIFECYCLE_INVALID")
            fixture = lifecycle.get("fixture_ci")
            if not isinstance(fixture, dict) or fixture != {
                "evidence_level": "AUTOMATED_FIXTURE_CI",
                "proves_formal_parity": False,
                "status": "CONFIGURED",
            }:
                errors.append("G04_FIXTURE_CI_BOUNDARY_INVALID")
            formal = lifecycle.get("formal_parity")
            if not isinstance(formal, dict) or set(formal) != {
                "evidence_level",
                "receipt_path",
                "status",
            }:
                errors.append("G04_FORMAL_PARITY_LIFECYCLE_INVALID")
            else:
                if formal.get("evidence_level") != "REAL_PADDLE_LICENSED_SCREENSHOT_PARITY":
                    errors.append("G04_FORMAL_PARITY_EVIDENCE_LEVEL_INVALID")
                if formal.get("receipt_path") != G04_FORMAL_RECEIPT_PATH:
                    errors.append("G04_FORMAL_PARITY_RECEIPT_PATH_INVALID")
                formal_status = formal.get("status")
                if formal_status not in {"NOT_RUN", "NOT_EVALUABLE", "FAIL", "PASS"}:
                    errors.append("G04_FORMAL_PARITY_STATUS_INVALID")
                lifecycle_state = lifecycle.get("state")
                if lifecycle_state not in {
                    "IMPLEMENTING",
                    "EVIDENCE_FROZEN",
                    "DELIVERY_PASS_RECORDED",
                    "TRANSITION_READY",
                }:
                    errors.append("G04_DELIVERY_EVIDENCE_STATE_INVALID")
                if lifecycle_state in {
                    "EVIDENCE_FROZEN",
                    "DELIVERY_PASS_RECORDED",
                    "TRANSITION_READY",
                } and formal_status != "PASS":
                    errors.append("G04_FORMAL_PARITY_REQUIRED_FOR_LIFECYCLE")
                formal_receipt_exists = (root / G04_FORMAL_RECEIPT_PATH).is_file()
                if formal_status == "PASS" and not formal_receipt_exists:
                    errors.append("G04_FORMAL_PARITY_RECEIPT_MISSING")
                if formal_status == "NOT_RUN" and formal_receipt_exists:
                    errors.append("G04_UNREGISTERED_FORMAL_PARITY_RECEIPT")
                if lifecycle_state in {
                    "DELIVERY_PASS_RECORDED",
                    "TRANSITION_READY",
                } and not (root / G04_PRODUCT_RECEIPT_PATH).is_file():
                    errors.append("G04_PRODUCT_DELIVERY_RECEIPT_MISSING")
                if goal_state.get("gate_result") == "PRODUCT_DELIVERY_PASS" and formal_status != "PASS":
                    errors.append("G04_FORMAL_PARITY_REQUIRED_FOR_DELIVERY_PASS")
            if lifecycle.get("transition_requires_formal_pass") is not True:
                errors.append("G04_TRANSITION_FORMAL_PASS_NOT_REQUIRED")

    packages = registry.get("packages")
    if not isinstance(packages, list) or not packages:
        raise WorkPackageV3Error("registry packages must be a non-empty list")
    package_ids = [item.get("package_id") for item in packages if isinstance(item, dict)]
    if len(package_ids) != len(packages) or len(set(package_ids)) != len(package_ids):
        errors.append("PACKAGE_IDS_NOT_UNIQUE")
    if package_id is not None and package_id not in package_ids:
        errors.append("PACKAGE_NOT_FOUND")
    if any(
        item.get("role") not in {"INTEGRATOR", "CONTRIBUTOR"}
        for item in packages
        if isinstance(item, dict)
    ):
        errors.append("PACKAGE_ROLE_INVALID")
    selected_package = next(
        (
            item
            for item in packages
            if isinstance(item, dict) and item.get("package_id") == package_id
        ),
        None,
    )

    for field in ("dialogue_ref", "branch", "remote_branch", "worktree_path"):
        values = [item.get(field) for item in packages if isinstance(item, dict)]
        if any(not isinstance(value, str) or not value for value in values):
            errors.append(f"PACKAGE_{field.upper()}_INVALID")
        elif len(values) != len(set(values)):
            errors.append(f"PACKAGE_{field.upper()}_NOT_UNIQUE")

    contributors = [
        item
        for item in packages
        if isinstance(item, dict) and item.get("role") == "CONTRIBUTOR"
    ]
    current_contributors = [
        item
        for item in contributors
        if item.get("goal_id") == registry.get("active_goal_id")
    ]
    next_goal_contributors = [
        item
        for item in contributors
        if item.get("goal_id") != registry.get("active_goal_id")
    ]
    max_prepared_next = registry.get("max_prepared_next_goal_packages")
    if (
        isinstance(max_prepared_next, bool)
        or not isinstance(max_prepared_next, int)
        or not 0 <= max_prepared_next <= 2
    ):
        errors.append("MAX_PREPARED_NEXT_GOAL_PACKAGES_INVALID")
    elif len(next_goal_contributors) > max_prepared_next:
        errors.append("MAX_PREPARED_NEXT_GOAL_PACKAGES_EXCEEDED")
    if any(
        item.get("goal_id") != registry.get("next_goal_id")
        for item in next_goal_contributors
    ):
        errors.append("NEXT_GOAL_PACKAGE_ID_MISMATCH")
    expected_order = [
        item.get("package_id")
        for item in sorted(
            current_contributors,
            key=lambda value: int(value.get("merge_order", 0)),
        )
    ]
    if registry.get("integration_order") != expected_order:
        errors.append("INTEGRATION_ORDER_MISMATCH")
    integrators = [
        item
        for item in packages
        if isinstance(item, dict) and item.get("role") == "INTEGRATOR"
    ]
    owner_review_hold = registry.get("program_state") == OWNER_REVIEW_PROGRAM_STATE
    expected_integrator_status = "MERGED" if owner_review_hold else "IN_PROGRESS"
    if (
        len(integrators) != 1
        or integrators[0].get("status") != expected_integrator_status
    ):
        errors.append("ACTIVE_INTEGRATOR_INVALID")
    elif integrators[0].get("goal_id") != registry.get("active_goal_id"):
        errors.append("ACTIVE_INTEGRATOR_GOAL_MISMATCH")
    active_contributors = [
        item
        for item in current_contributors
        if item.get("status") in ACTIVE_CONTRIBUTOR_STATUSES
    ]
    if len(active_contributors) > 1:
        errors.append("PARALLEL_WRITER_LIMIT_EXCEEDED")
    expected_writer_activation = "NONE" if owner_review_hold else (
        "INTEGRATOR_AND_CONTRIBUTOR"
        if active_contributors
        else "INTEGRATOR_ONLY"
    )
    if registry.get("writer_activation") != expected_writer_activation:
        errors.append("WRITER_ACTIVATION_MISMATCH")

    phase = active_slice.get("phase") if isinstance(active_slice, dict) else None
    if phase not in LEGAL_PHASES:
        errors.append("ACTIVE_SLICE_PHASE_INVALID")

    contributor_owned: list[tuple[str, list[str]]] = []
    for package in contributors:
        package_name = str(package.get("package_id"))
        status = package.get("status")
        if status not in PACKAGE_STATUSES:
            errors.append(f"{package_name}:STATUS_INVALID")
        if (
            package.get("goal_id") != registry.get("active_goal_id")
            and status != "PREPARED_NOT_INTEGRATED"
        ):
            errors.append(f"{package_name}:NEXT_GOAL_STATUS_INVALID")
        if status in {
            "IN_PROGRESS",
            "BLOCKED_EXTERNAL",
            "READY_TO_MERGE",
            "MERGED",
        }:
            errors.extend(_registry_binding_snapshot_errors(root, package))
        prompt_path = package.get("prompt_path")
        prompt_hash = package.get("prompt_sha256")
        if not isinstance(prompt_path, str) or not isinstance(prompt_hash, str):
            errors.append(f"{package_name}:PROMPT_BINDING_MISSING")
        else:
            try:
                if _sha256(root / prompt_path) != prompt_hash:
                    errors.append(f"{package_name}:PROMPT_HASH_MISMATCH")
            except OSError:
                errors.append(f"{package_name}:PROMPT_PATH_MISSING")
        owned = package.get("owned_paths")
        forbidden = package.get("forbidden_paths")
        if not isinstance(owned, list) or not owned or not all(
            isinstance(item, str) and item for item in owned
        ):
            errors.append(f"{package_name}:OWNED_PATHS_INVALID")
            continue
        if not isinstance(forbidden, list) or not all(
            isinstance(item, str) and item for item in forbidden
        ):
            errors.append(f"{package_name}:FORBIDDEN_PATHS_INVALID")
            forbidden = []
        if any(
            _is_within(path, forbidden) or _is_within(blocked, owned)
            for path in owned
            for blocked in forbidden
        ):
            errors.append(f"{package_name}:OWNED_FORBIDDEN_OVERLAP")
        for other_name, other_owned in contributor_owned:
            if any(
                _is_within(path, other_owned) or _is_within(other, owned)
                for path in owned
                for other in other_owned
            ):
                errors.append(f"{package_name}:{other_name}:OWNED_PATHS_OVERLAP")
        contributor_owned.append((package_name, owned))

    if integrators:
        integrator = integrators[0]
        integrator_name = str(integrator.get("package_id"))
        integrator_owned = integrator.get("owned_paths")
        if not isinstance(integrator_owned, list) or not all(
            isinstance(item, str) and item for item in integrator_owned
        ):
            errors.append(f"{integrator_name}:OWNED_PATHS_INVALID")
        else:
            for contributor_name, owned in contributor_owned:
                if any(
                    _is_within(path, integrator_owned)
                    or _is_within(integrator_path, owned)
                    for path in owned
                    for integrator_path in integrator_owned
                ):
                    errors.append(
                        f"{integrator_name}:{contributor_name}:OWNED_PATHS_OVERLAP"
                    )

    if selected_package is not None:
        selected_name = str(selected_package.get("package_id"))
        if selected_package.get("role") != "CONTRIBUTOR":
            errors.append(f"{selected_name}:CHECKOUT_ROLE_INVALID")
        if selected_package.get("status") not in {
            "IN_PROGRESS",
            "BLOCKED_EXTERNAL",
            "READY_TO_MERGE",
        }:
            errors.append(f"{selected_name}:CHECKOUT_STATUS_INVALID")
        expected_worktree = selected_package.get("worktree_path")
        if not isinstance(expected_worktree, str) or (
            _normalized_path(root) != _normalized_path(Path(expected_worktree))
        ):
            errors.append(f"{selected_name}:CHECKOUT_WORKTREE_MISMATCH")
        expected_branch = selected_package.get("branch")
        current_branch = _git(
            root, "symbolic-ref", "--short", "-q", "HEAD", check=False
        )
        if current_branch != expected_branch:
            errors.append(f"{selected_name}:CHECKOUT_BRANCH_MISMATCH")
        if _git(root, "status", "--porcelain", check=False):
            errors.append(f"{selected_name}:CHECKOUT_NOT_CLEAN")
        prompt_path = selected_package.get("prompt_path")
        if not isinstance(prompt_path, str) or not _git_succeeds(
            root, "ls-files", "--error-unmatch", "--", prompt_path
        ):
            errors.append(f"{selected_name}:PROMPT_NOT_TRACKED")
        for field in ("registry_activation_commit", "registry_binding_commit"):
            commit = selected_package.get(field)
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                errors.append(f"{selected_name}:{field.upper()}_INVALID")
            elif not _git_succeeds(
                root, "merge-base", "--is-ancestor", commit, "HEAD"
            ):
                errors.append(f"{selected_name}:{field.upper()}_NOT_ANCESTOR")

    first_parent = _git(
        root,
        "rev-list",
        "--first-parent",
        "--reverse",
        head_ref,
    ).splitlines()
    merged_positions: dict[str, int] = {}

    for package in current_contributors:
        package_name = str(package.get("package_id"))
        status = package.get("status")
        if (
            (require_all_merged or phase in LEGAL_PHASES - DEVELOPMENT_PHASES)
            and status != "MERGED"
        ):
            errors.append(f"{package_name}:NOT_MERGED_FOR_FINAL_EVIDENCE")
        if status not in {"READY_TO_MERGE", "MERGED"}:
            continue
        ready = package.get("ready_commit")
        merged = package.get("merged_commit")
        remote = package.get("remote_branch")
        branch = package.get("branch")
        required_commits = (ready, remote) if status == "READY_TO_MERGE" else (
            ready,
            merged,
            remote,
        )
        if not all(isinstance(value, str) and value for value in required_commits):
            errors.append(f"{package_name}:COMMIT_BINDING_MISSING")
            continue
        if not isinstance(branch, str) or remote != f"origin/{branch}":
            errors.append(f"{package_name}:REMOTE_BRANCH_BINDING_INVALID")
        elif require_all_merged:
            if _remote_branch_tip(root, branch) != ready:
                errors.append(f"{package_name}:REMOTE_READBACK_MISMATCH")
        elif _git(root, "rev-parse", remote, check=False) != ready:
            errors.append(f"{package_name}:LOCAL_REMOTE_TRACKING_MISMATCH")
        branch_point = package.get("branch_point_commit")
        activation_commit = package.get("registry_activation_commit")
        binding_commit = package.get("registry_binding_commit")
        exact_chain = (activation_commit, binding_commit, branch_point)
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
            for value in exact_chain
        ):
            errors.append(f"{package_name}:BRANCH_POINT_COMMIT_MISSING")
        else:
            assert isinstance(activation_commit, str)
            assert isinstance(binding_commit, str)
            assert isinstance(branch_point, str)
            if not (
                _git_succeeds(
                    root,
                    "merge-base",
                    "--is-ancestor",
                    activation_commit,
                    binding_commit,
                )
                and _git_succeeds(
                    root,
                    "merge-base",
                    "--is-ancestor",
                    binding_commit,
                    branch_point,
                )
                and _git_succeeds(
                    root,
                    "merge-base",
                    "--is-ancestor",
                    branch_point,
                    head_ref,
                )
            ):
                errors.append(f"{package_name}:FEATURE_ANCESTRY_INVALID")
            parent_line = _git(
                root,
                "rev-list",
                "--parents",
                "-n",
                "1",
                ready,
                check=False,
            ).split()
            if len(parent_line) != 2 or parent_line[1] != branch_point:
                errors.append(f"{package_name}:FEATURE_COMMIT_COUNT_INVALID")
        owned = package.get("owned_paths")
        forbidden = package.get("forbidden_paths")
        if not isinstance(owned, list) or not all(isinstance(item, str) for item in owned):
            errors.append(f"{package_name}:OWNED_PATHS_INVALID")
            continue
        forbidden_values = (
            forbidden if isinstance(forbidden, list) and all(isinstance(item, str) for item in forbidden) else []
        )
        commits_to_check = [("READY", ready)]
        if status == "MERGED":
            assert isinstance(merged, str)
            commits_to_check.append(("MERGED", merged))
        for label, commit in commits_to_check:
            try:
                paths = _commit_paths(root, commit)
            except WorkPackageV3Error:
                errors.append(f"{package_name}:{label}_COMMIT_UNAVAILABLE")
                continue
            if any(not _is_within(path, owned) for path in paths):
                errors.append(f"{package_name}:{label}_OUTSIDE_OWNED_PATHS")
            if any(_is_within(path, forbidden_values) for path in paths):
                errors.append(f"{package_name}:{label}_TOUCHED_FORBIDDEN_PATH")
        if status == "READY_TO_MERGE":
            continue
        assert isinstance(merged, str)
        try:
            if _stable_patch_id(root, ready) != _stable_patch_id(root, merged):
                errors.append(f"{package_name}:PATCH_ID_MISMATCH")
        except WorkPackageV3Error:
            errors.append(f"{package_name}:PATCH_ID_UNAVAILABLE")
        if merged not in first_parent:
            errors.append(f"{package_name}:MERGED_COMMIT_NOT_FIRST_PARENT")
        else:
            merged_positions[package_name] = first_parent.index(merged)

    merged_package_names = [
        str(item.get("package_id"))
        for item in current_contributors
        if item.get("status") == "MERGED"
    ]
    ordered_positions = [
        merged_positions[item]
        for item in merged_package_names
        if item in merged_positions
    ]
    expected_merged_prefix = expected_order[: len(merged_package_names)]
    if (
        merged_package_names != expected_merged_prefix
        or ordered_positions != sorted(ordered_positions)
        or len(ordered_positions) != len(merged_package_names)
    ):
        errors.append("CONTRIBUTOR_MERGE_ORDER_INVALID")

    changed_paths: tuple[str, ...] = ()
    scope_base_commit: str | None = None
    if not isinstance(active_slice, dict):
        errors.append("ACTIVE_SLICE_INVALID")
    else:
        allowed = active_slice.get("allowed_paths")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            errors.append("ACTIVE_SLICE_ALLOWED_PATHS_INVALID")
        else:
            if any(
                item.startswith(FROZEN_AGENT_GATE_PREFIX)
                and not _agent_gate_path_is_authorized_for_g07(
                    item,
                    registry=registry,
                    active_slice=active_slice,
                )
                for item in allowed
            ):
                errors.append("FROZEN_AGENT_GATE_PATH_AUTHORIZED")
            if registry.get("active_goal_id") == G04_GOAL_ID:
                for required_path in (
                    G04_FORMAL_VALIDATOR_PATH,
                    G04_FORMAL_RECEIPT_PATH,
                    G04_PRODUCT_RECEIPT_PATH,
                ):
                    if required_path not in allowed:
                        errors.append("G04_DELIVERY_EVIDENCE_PATH_NOT_AUTHORIZED")
            if check_scope:
                scope_allowed = allowed
                scope_package = selected_package
                if scope_package is None:
                    current_branch = _git(
                        root, "symbolic-ref", "--short", "-q", "HEAD", check=False
                    )
                    scope_package = next(
                        (
                            item
                            for item in contributors
                            if item.get("branch") == current_branch
                        ),
                        None,
                    )
                if scope_package is not None:
                    package_owned = scope_package.get("owned_paths")
                    canonical_ref = binding.get("canonical_candidate_ref")
                    if not isinstance(package_owned, list) or not all(
                        isinstance(item, str) and item for item in package_owned
                    ):
                        errors.append("PACKAGE_SCOPE_OWNED_PATHS_INVALID")
                    elif not isinstance(canonical_ref, str) or not canonical_ref:
                        errors.append("PACKAGE_SCOPE_CANONICAL_REF_INVALID")
                    else:
                        scope_allowed = package_owned
                        scope_base_commit = _git(
                            root,
                            "merge-base",
                            "HEAD",
                            canonical_ref,
                            check=False,
                        )
                        if not scope_base_commit:
                            errors.append("PACKAGE_SCOPE_BRANCH_POINT_MISSING")
                else:
                    base_commit = active_slice.get("base_commit")
                    if isinstance(base_commit, str) and base_commit:
                        scope_base_commit = base_commit
                if not scope_base_commit:
                    errors.append("ACTIVE_SLICE_BASE_INVALID")
                else:
                    changed_paths = _working_paths(root, scope_base_commit)
                    if any(
                        not _is_within(path, scope_allowed) for path in changed_paths
                    ):
                        errors.append("ACTIVE_SLICE_SCOPE_VIOLATION")
                    if any(
                        path.startswith(FROZEN_AGENT_GATE_PREFIX)
                        and not _agent_gate_path_is_authorized_for_g07(
                            path,
                            registry=registry,
                            active_slice=active_slice,
                        )
                        for path in changed_paths
                    ):
                        errors.append("FROZEN_AGENT_GATE_CHANGED")

    return {
        "schema_version": "work-package-validation-v3",
        "active_goal_id": registry.get("active_goal_id"),
        "package_count": len(packages),
        "package_id": package_id,
        "scope_base_commit": scope_base_commit,
        "changed_paths": list(changed_paths),
        "error_codes": sorted(set(errors)),
        "verdict": "PASS" if not errors else "FAIL",
    }
