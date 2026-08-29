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


WORK_PACKAGE_PROMPT_SCHEMA_VERSION = "work-package-prompt-v1"
WORK_PACKAGE_PROMPT_REQUIRED_SECTIONS: tuple[str, ...] = (
    "# Work Package Prompt v1",
    "## Identity and exact baseline",
    "## Branch and isolated worktree",
    "## Owned and forbidden paths",
    "## User-observable outcome",
    "## Non-goals and locked contracts",
    "## Dependencies, inputs and outputs",
    "## Acceptance and targeted verification",
    "## Git and authority restrictions",
    "## Subagent boundary",
    "## Completion report",
)


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


def _validate_prompt_contract_bytes(
    value: bytes,
    package: WorkPackageBinding,
) -> None:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkPackageValidationError("work package prompt must be UTF-8") from exc
    missing_sections = [
        section for section in WORK_PACKAGE_PROMPT_REQUIRED_SECTIONS if section not in text
    ]
    if missing_sections:
        raise WorkPackageValidationError(
            "work package prompt is incomplete: " + json.dumps(missing_sections)
        )
    required_bindings = {
        "prompt_schema_version": WORK_PACKAGE_PROMPT_SCHEMA_VERSION,
        "package_id": package.package_id,
        "goal_id": package.goal_id,
        "role": package.role,
        "baseline_commit": package.baseline_commit,
        "registry_activation_commit": package.registry_activation_commit,
        "branch": package.branch,
        "remote_branch": package.remote_branch,
        "worktree_path": package.worktree_path,
    }
    for key, expected in required_bindings.items():
        if expected is None or f"{key}: {expected}" not in text:
            raise WorkPackageValidationError(
                f"work package prompt is missing exact {key} binding"
            )
    for path in (*package.owned_paths, *package.forbidden_paths):
        if f"- {path}" not in text:
            raise WorkPackageValidationError(
                f"work package prompt does not enumerate governed path: {path}"
            )
    for required_token in (
        "READY_TO_MERGE",
        "IN_PROGRESS",
        "BLOCKED_EXTERNAL",
        "remote readback",
        "subagent_read_only: true",
        "must_not_merge: true",
        "must_not_modify_goal_or_registry: true",
    ):
        if required_token not in text:
            raise WorkPackageValidationError(
                f"work package prompt is missing required restriction: {required_token}"
            )


def _validate_prompt_file(repository_root: Path, package: WorkPackageBinding) -> None:
    if package.role != "CONTRIBUTOR":
        return
    if package.prompt_path is None or package.prompt_sha256 is None:
        raise WorkPackageValidationError("contributor prompt binding is incomplete")
    prompt_path = repository_root / package.prompt_path
    try:
        prompt_bytes = prompt_path.read_bytes()
    except OSError as exc:
        raise WorkPackageValidationError("registered work package prompt is absent") from exc
    if _sha256_bytes(prompt_bytes) != package.prompt_sha256:
        raise WorkPackageValidationError(
            "registered work package prompt hash differs; checkout is read-only"
        )
    _validate_prompt_contract_bytes(prompt_bytes, package)


def _validate_prompt_blob(
    repository_root: Path,
    commit: str,
    package: WorkPackageBinding,
) -> None:
    if package.role != "CONTRIBUTOR":
        return
    if package.prompt_path is None or package.prompt_sha256 is None:
        raise WorkPackageValidationError("candidate contributor prompt binding is incomplete")
    prompt_bytes = _git_blob(repository_root, commit, package.prompt_path)
    if _sha256_bytes(prompt_bytes) != package.prompt_sha256:
        raise WorkPackageValidationError("candidate work package prompt hash differs")
    _validate_prompt_contract_bytes(prompt_bytes, package)


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
    if registry.schema_version != "work-package-registry-v2":
        raise WorkPackageValidationError(
            "work package registry is not v2; checkout is read-only"
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
    for package in registry.packages:
        if package.goal_id == registry.active_goal_id:
            _validate_prompt_file(repository_root, package)
    return registry


def _is_ancestor(repository_root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _validate_candidate_integration_history(
    repository_root: Path,
    candidate_commit: str,
    registry: WorkPackageRegistry,
) -> None:
    known = {item.package_id: item for item in registry.packages}
    previous_merge: str | None = None
    for package_id in registry.integration_order:
        package = known[package_id]
        if package.status == "DEFERRED":
            if registry.active_goal_id == "TC-VNEXT-G02-MAP-STAY":
                raise WorkPackageValidationError(
                    "G02 required function packages cannot be deferred"
                )
            continue
        if package.status != "MERGED" or package.merged_commit is None:
            raise WorkPackageValidationError(
                "candidate Gate cannot run before serial package integration"
            )
        if package.ready_commit is None or not _is_ancestor(
            repository_root,
            package.ready_commit,
            package.merged_commit,
        ):
            raise WorkPackageValidationError(
                f"package merge does not contain frozen ready commit: {package_id}"
            )
        if package.remote_branch is None:
            raise WorkPackageValidationError(
                f"merged package has no remote branch binding: {package_id}"
            )
        remote_ref = f"refs/remotes/{package.remote_branch}"
        if _git(repository_root, "rev-parse", "--verify", remote_ref) != (
            package.ready_commit
        ):
            raise WorkPackageValidationError(
                f"package branch advanced after READY_TO_MERGE: {package_id}"
            )
        if previous_merge is not None and not _is_ancestor(
            repository_root,
            previous_merge,
            package.merged_commit,
        ):
            raise WorkPackageValidationError(
                "package merge history violates the registered integration order"
            )
        if not _is_ancestor(repository_root, package.merged_commit, candidate_commit):
            raise WorkPackageValidationError(
                f"candidate does not contain registered package merge: {package_id}"
            )
        previous_merge = package.merged_commit


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
    if registry.schema_version != "work-package-registry-v2":
        raise WorkPackageValidationError(
            "candidate work package registry must be v2"
        )
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
    for package in registry.packages:
        if package.goal_id == registry.active_goal_id:
            _validate_prompt_blob(repository_root, candidate_commit, package)
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
        _validate_candidate_integration_history(
            repository_root,
            candidate_commit,
            registry,
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
    if package.status != "IN_PROGRESS":
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
    if package.role == "CONTRIBUTOR":
        if package.registry_activation_commit is None:
            raise WorkPackageValidationError(
                "contributor registry activation commit is absent"
            )
        if not _is_ancestor(
            repository_root,
            package.baseline_commit,
            package.registry_activation_commit,
        ) or not _is_ancestor(
            repository_root,
            package.registry_activation_commit,
            activation_commit,
        ):
            raise WorkPackageValidationError(
                "contributor prompt does not bind the active registry ancestry"
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
    if package.worktree_path is None or (
        repository_root.resolve().as_posix().casefold()
        != package.worktree_path.rstrip("/").casefold()
    ):
        raise WorkPackageValidationError(
            "checkout path differs from the registered independent worktree"
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


def _worktree_records(repository_root: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in _git(repository_root, "worktree", "list", "--porcelain").splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def validate_ready_to_merge_package(
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
    if package.role != "CONTRIBUTOR" or package.status != "READY_TO_MERGE":
        raise WorkPackageValidationError("work package is not frozen READY_TO_MERGE")
    if (
        package.worktree_path is None
        or package.ready_commit is None
        or package.remote_branch is None
    ):
        raise WorkPackageValidationError("ready package binding is incomplete")
    expected_path = package.worktree_path.rstrip("/").casefold()
    record = next(
        (
            item
            for item in _worktree_records(repository_root)
            if item.get("worktree", "").replace("\\", "/").rstrip("/").casefold()
            == expected_path
        ),
        None,
    )
    if record is None:
        raise WorkPackageValidationError("registered ready worktree is absent")
    if record.get("branch") != f"refs/heads/{package.branch}":
        raise WorkPackageValidationError("registered ready worktree branch differs")
    worktree_root = Path(record["worktree"])
    if _git(worktree_root, "rev-parse", "HEAD") != package.ready_commit:
        raise WorkPackageValidationError(
            "READY_TO_MERGE branch tip changed after freeze"
        )
    if _git(worktree_root, "status", "--porcelain"):
        raise WorkPackageValidationError(
            "READY_TO_MERGE worktree became dirty after freeze"
        )
    remote_ref = f"refs/remotes/{package.remote_branch}"
    if _git(repository_root, "rev-parse", "--verify", remote_ref) != package.ready_commit:
        raise WorkPackageValidationError(
            "READY_TO_MERGE remote readback differs from ready_commit"
        )
    return package
