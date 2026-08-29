from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from evals.agent_gate_v1.contracts import (
    CurrentGoalBinding,
    WorkPackageBinding,
    WorkPackageRegistry,
)


class WorkPackageValidationError(ValueError):
    pass


def _git(repository_root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise WorkPackageValidationError(
            result.stderr.strip() or f"git {' '.join(args)} failed"
        )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_blob(repository_root: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise WorkPackageValidationError(
            f"candidate governance blob is absent: {path}"
        )
    return result.stdout


def _contains(root: str, candidate: str) -> bool:
    return candidate == root or candidate.startswith(f"{root}/")


def load_work_package_registry(repository_root: Path) -> WorkPackageRegistry:
    registry_path = repository_root / "docs/governance/current_work_packages.json"
    binding_path = repository_root / "docs/governance/current_goal_binding.json"
    agents_path = repository_root / "AGENTS.md"
    try:
        registry = WorkPackageRegistry.model_validate_json(registry_path.read_bytes())
        binding = CurrentGoalBinding.model_validate_json(binding_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise WorkPackageValidationError(
            "work package governance files are absent or invalid; checkout is read-only"
        ) from exc

    if binding.schema_version != "current-goal-binding-v2":
        raise WorkPackageValidationError(
            "current Goal binding is not v2; checkout is read-only"
        )
    if binding.work_package_registry_path != (
        "docs/governance/current_work_packages.json"
    ):
        raise WorkPackageValidationError(
            "current Goal does not bind the work package registry"
        )
    if (
        registry.active_goal_sequence != binding.goal_sequence
        or registry.active_goal_id != binding.goal_id
        or registry.mainline_phase != binding.mainline_phase
        or registry.gate_profile != binding.gate_profile
    ):
        raise WorkPackageValidationError(
            "work package registry and current Goal binding disagree; checkout is read-only"
        )
    if _sha256(agents_path) != registry.guidance_sha256:
        raise WorkPackageValidationError(
            "AGENTS.md differs from the registered guidance; checkout is read-only"
        )
    return registry


def load_candidate_work_package_registry(
    repository_root: Path,
    candidate_commit: str,
    binding: CurrentGoalBinding,
    *,
    require_gate_ready: bool = True,
) -> tuple[WorkPackageRegistry, str]:
    if binding.schema_version != "current-goal-binding-v2":
        raise WorkPackageValidationError(
            "active candidate Goal binding must be v2"
        )
    registry_path = binding.work_package_registry_path
    if registry_path != "docs/governance/current_work_packages.json":
        raise WorkPackageValidationError(
            "active candidate does not bind the work package registry"
        )
    registry_bytes = _git_blob(repository_root, candidate_commit, registry_path)
    try:
        registry = WorkPackageRegistry.model_validate_json(registry_bytes)
    except ValueError as exc:
        raise WorkPackageValidationError(
            "candidate work package registry is invalid"
        ) from exc
    if (
        registry.active_goal_sequence != binding.goal_sequence
        or registry.active_goal_id != binding.goal_id
        or registry.mainline_phase != binding.mainline_phase
        or registry.gate_profile != binding.gate_profile
    ):
        raise WorkPackageValidationError(
            "candidate registry and current Goal binding disagree"
        )
    agents_bytes = _git_blob(repository_root, candidate_commit, "AGENTS.md")
    if _sha256_bytes(agents_bytes) != registry.guidance_sha256:
        raise WorkPackageValidationError(
            "candidate AGENTS.md differs from the registered guidance"
        )
    if require_gate_ready:
        unfinished_contributors = [
            item.package_id
            for item in registry.packages
            if item.goal_id == registry.active_goal_id
            and item.role == "CONTRIBUTOR"
            and item.status not in {"MERGED", "DEFERRED"}
        ]
        if unfinished_contributors:
            raise WorkPackageValidationError(
                "candidate Gate cannot run before contributor integration: "
                + json.dumps(sorted(unfinished_contributors))
            )
    return registry, _sha256_bytes(registry_bytes)


def _changed_paths(repository_root: Path, baseline_commit: str) -> set[str]:
    commands = (
        ("diff", "--name-only", f"{baseline_commit}...HEAD"),
        ("diff", "--name-only"),
        ("diff", "--cached", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    )
    changed: set[str] = set()
    for command in commands:
        for path in _git(repository_root, *command).splitlines():
            normalized = path.strip().replace("\\", "/")
            if normalized:
                changed.add(normalized)
    return changed


def _registry_activation_commit(repository_root: Path) -> str:
    commit = _git(
        repository_root,
        "log",
        "-1",
        "--format=%H",
        "HEAD",
        "--",
        "docs/governance/current_work_packages.json",
    )
    if not commit:
        raise WorkPackageValidationError(
            "work package registry has no committed activation point"
        )
    return commit


def validate_package_checkout(
    repository_root: Path,
    package_id: str,
) -> WorkPackageBinding:
    registry = load_work_package_registry(repository_root)
    package = next(
        (item for item in registry.packages if item.package_id == package_id),
        None,
    )
    if package is None:
        raise WorkPackageValidationError("work package is not registered")
    if package.status not in {"PREPARED_NOT_INTEGRATED", "IN_PROGRESS"}:
        raise WorkPackageValidationError("work package is not writable")

    activation_commit = _registry_activation_commit(repository_root)
    declared_baseline = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            package.baseline_commit,
            activation_commit,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if declared_baseline.returncode != 0:
        raise WorkPackageValidationError(
            "registry activation does not descend from the declared Goal baseline"
        )
    activation_ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            activation_commit,
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if activation_ancestor.returncode != 0:
        raise WorkPackageValidationError(
            "checkout does not descend from the registry activation commit"
        )

    branch = _git(repository_root, "branch", "--show-current")
    if branch != package.branch:
        raise WorkPackageValidationError(
            "checkout branch differs from the registered work package branch"
        )

    changed = _changed_paths(repository_root, activation_commit)
    allowed_roots = list(package.owned_paths)
    if package.role == "INTEGRATOR":
        allowed_roots.extend(
            owned_path
            for item in registry.packages
            if item.goal_id == registry.active_goal_id
            and item.role == "CONTRIBUTOR"
            and item.status == "MERGED"
            for owned_path in item.owned_paths
        )
    outside = sorted(
        path
        for path in changed
        if not any(_contains(root, path) for root in allowed_roots)
    )
    forbidden = sorted(
        path
        for path in changed
        if any(_contains(root, path) for root in package.forbidden_paths)
    )
    if outside:
        raise WorkPackageValidationError(
            f"changed paths fall outside package ownership: {outside}"
        )
    if forbidden:
        raise WorkPackageValidationError(
            f"changed paths touch integrator-owned files: {forbidden}"
        )
    return package
