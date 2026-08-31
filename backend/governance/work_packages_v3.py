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
        in {"G07-CANDIDATE-CONTRACT", "G07-COMPONENT-RAW-REVALIDATION"}
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
    if registry.get("guidance_sha256") != _sha256(root / GUIDANCE_PATH):
        errors.append("GUIDANCE_HASH_MISMATCH")
    if registry.get("scope_policy_sha256") != _sha256(root / POLICY_PATH):
        errors.append("SCOPE_POLICY_HASH_MISMATCH")
    if registry.get("max_parallel_writers") != 2:
        errors.append("PARALLEL_WRITER_LIMIT_INVALID")

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
    expected_order = [
        item.get("package_id")
        for item in sorted(contributors, key=lambda value: int(value.get("merge_order", 0)))
    ]
    if registry.get("integration_order") != expected_order:
        errors.append("INTEGRATION_ORDER_MISMATCH")
    first_parent = _git(
        root,
        "rev-list",
        "--first-parent",
        "--reverse",
        head_ref,
    ).splitlines()
    merged_positions: dict[str, int] = {}

    for package in contributors:
        package_name = str(package.get("package_id"))
        if package.get("status") != "MERGED":
            errors.append(f"{package_name}:NOT_MERGED")
            continue
        prompt_path = package.get("prompt_path")
        prompt_hash = package.get("prompt_sha256")
        if not isinstance(prompt_path, str) or not isinstance(prompt_hash, str):
            errors.append(f"{package_name}:PROMPT_BINDING_MISSING")
        elif _sha256(root / prompt_path) != prompt_hash:
            errors.append(f"{package_name}:PROMPT_HASH_MISMATCH")
        ready = package.get("ready_commit")
        merged = package.get("merged_commit")
        remote = package.get("remote_branch")
        if not all(isinstance(value, str) and value for value in (ready, merged, remote)):
            errors.append(f"{package_name}:COMMIT_BINDING_MISSING")
            continue
        if _git(root, "rev-parse", remote, check=False) != ready:
            errors.append(f"{package_name}:REMOTE_TIP_MOVED")
        try:
            if _stable_patch_id(root, ready) != _stable_patch_id(root, merged):
                errors.append(f"{package_name}:PATCH_ID_MISMATCH")
        except WorkPackageV3Error:
            errors.append(f"{package_name}:PATCH_ID_UNAVAILABLE")
        owned = package.get("owned_paths")
        forbidden = package.get("forbidden_paths")
        if not isinstance(owned, list) or not all(isinstance(item, str) for item in owned):
            errors.append(f"{package_name}:OWNED_PATHS_INVALID")
            continue
        forbidden_values = (
            forbidden if isinstance(forbidden, list) and all(isinstance(item, str) for item in forbidden) else []
        )
        for label, commit in (("READY", ready), ("MERGED", merged)):
            try:
                paths = _commit_paths(root, commit)
            except WorkPackageV3Error:
                errors.append(f"{package_name}:{label}_COMMIT_UNAVAILABLE")
                continue
            if any(not _is_within(path, owned) for path in paths):
                errors.append(f"{package_name}:{label}_OUTSIDE_OWNED_PATHS")
            if any(_is_within(path, forbidden_values) for path in paths):
                errors.append(f"{package_name}:{label}_TOUCHED_FORBIDDEN_PATH")
        if merged not in first_parent:
            errors.append(f"{package_name}:MERGED_COMMIT_NOT_FIRST_PARENT")
        else:
            merged_positions[package_name] = first_parent.index(merged)

    ordered_positions = [
        merged_positions[item]
        for item in expected_order
        if item in merged_positions
    ]
    if ordered_positions != sorted(ordered_positions) or len(ordered_positions) != len(contributors):
        errors.append("CONTRIBUTOR_MERGE_ORDER_INVALID")

    active_slice = registry.get("active_slice")
    changed_paths: tuple[str, ...] = ()
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
                base_commit = active_slice.get("base_commit")
                if not isinstance(base_commit, str) or not base_commit:
                    errors.append("ACTIVE_SLICE_BASE_INVALID")
                else:
                    changed_paths = _working_paths(root, base_commit)
                    if any(not _is_within(path, allowed) for path in changed_paths):
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
        "changed_paths": list(changed_paths),
        "error_codes": sorted(set(errors)),
        "verdict": "PASS" if not errors else "FAIL",
    }
