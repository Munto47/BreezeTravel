from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONTRACT_PATH = "docs/governance/product_delivery_gates.json"
BINDING_PATH = "docs/governance/current_goal_binding.json"
REGISTRY_PATH = "docs/governance/current_work_packages.json"
GUIDANCE_PATH = "AGENTS.md"
CURRENT_GOAL_PATH = "docs/governance/CURRENT_GOAL.md"
OWNER_REVIEW_STATE = "CORE_MVP_OWNER_REVIEW_PENDING"
OWNER_REVIEW_STATUS = "OWNER_REVIEW_PENDING"
G03_GOAL_ID = "TC-VNEXT-G03-TOP3-AUDIT"
G04_GOAL_ID = "TC-VNEXT-G04-SCREENSHOT"
G03_REPAIR_OWNER_AUTHORIZATION = "OWNER_APPROVED_G03_P1_REPAIR_2026-08-30"
G03_REPAIR_SEMANTIC_PROMPT_PATH = (
    "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md"
)

PRODUCT_ROOTS = (
    "backend/app/",
    "frontend/src/",
    "miniapp/src/",
    "packages/trip-check-client/src/",
)
PRODUCT_CONFIG_PATHS = {
    ".env.example",
    "backend/requirements-base.txt",
    "backend/requirements.txt",
    "docker-compose.yml",
    "frontend/next.config.js",
    "frontend/package-lock.json",
    "frontend/package.json",
    "miniapp/package-lock.json",
    "miniapp/package.json",
    "packages/trip-check-client/package-lock.json",
    "packages/trip-check-client/package.json",
}
GOVERNANCE_PREFIXES = ("docs/governance/", ".github/")
GOVERNANCE_EXACT = {
    GUIDANCE_PATH,
    "docs/product/PROJECT_CHARTER.md",
    "docs/product/TRIP_CHECK_SPEC.md",
}


class CoreMainlineError(ValueError):
    pass


_PRODUCT_GOAL_STATE_PATTERN = re.compile(
    r"<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE\n(?P<payload>\{.*?\})\n-->",
    re.DOTALL,
)


@dataclass(frozen=True)
class CoreMainlineReport:
    verdict: str
    goal_id: str
    goal_sequence: int
    delivery_goal_sequence: int
    work_kind: str
    phase: str
    changed_files: tuple[str, ...]
    product_progress: tuple[str, ...]
    product_fingerprint: str
    frozen_g07_changes: tuple[str, ...]
    deferred_work_changes: tuple[str, ...]
    owner_review_required: bool
    bootstrap_correction: bool
    errors: tuple[str, ...]

    def model_dump(self) -> dict[str, Any]:
        return {
            "schema_version": "core-mainline-report-v1",
            "verdict": self.verdict,
            "goal_id": self.goal_id,
            "goal_sequence": self.goal_sequence,
            "delivery_goal_sequence": self.delivery_goal_sequence,
            "work_kind": self.work_kind,
            "phase": self.phase,
            "changed_files": list(self.changed_files),
            "product_progress": list(self.product_progress) or ["NONE"],
            "product_fingerprint": self.product_fingerprint,
            "frozen_g07_changes": list(self.frozen_g07_changes),
            "deferred_work_changes": list(self.deferred_work_changes),
            "owner_review_required": self.owner_review_required,
            "bootstrap_correction": self.bootstrap_correction,
            "errors": list(self.errors),
        }


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads((root / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoreMainlineError(f"invalid or missing governance file: {relative}") from exc
    if not isinstance(value, dict):
        raise CoreMainlineError(f"governance file must contain an object: {relative}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        raise CoreMainlineError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _git_object_exists(root: Path, object_name: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", object_name],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _read_json_at_ref(
    root: Path,
    ref: str,
    relative: str,
) -> dict[str, Any] | None:
    object_name = f"{ref}:{relative}"
    if not _git_object_exists(root, object_name):
        return None
    try:
        value = json.loads(_git(root, "show", object_name))
    except json.JSONDecodeError as exc:
        raise CoreMainlineError(
            f"governance file at base ref is not valid JSON: {relative}"
        ) from exc
    if not isinstance(value, dict):
        raise CoreMainlineError(
            f"governance file at base ref must contain an object: {relative}"
        )
    return value


def _normalized_paths(lines: str) -> tuple[str, ...]:
    return tuple(sorted({line.strip().replace("\\", "/") for line in lines.splitlines() if line.strip()}))


def changed_paths(root: Path, base_ref: str, head_ref: str = "HEAD") -> tuple[str, ...]:
    if not _git_object_exists(root, base_ref):
        raise CoreMainlineError(
            f"base ref is unavailable: {base_ref}; CI must use a full-history checkout"
        )
    if not _git_object_exists(root, head_ref):
        raise CoreMainlineError(f"head ref is unavailable: {head_ref}")
    return _normalized_paths(_git(root, "diff", "--name-only", base_ref, head_ref, "--"))


def _tracked_product_paths(root: Path) -> tuple[str, ...]:
    tracked = _normalized_paths(_git(root, "ls-files", "--", *PRODUCT_ROOTS, *sorted(PRODUCT_CONFIG_PATHS)))
    untracked = _normalized_paths(
        _git(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            *PRODUCT_ROOTS,
            *sorted(PRODUCT_CONFIG_PATHS),
        )
    )
    return tuple(sorted(set(tracked) | set(untracked)))


def product_fingerprint(root: Path) -> str:
    """Bind evidence to Git-clean product bytes, independent of checkout EOLs."""

    digest = hashlib.sha256()
    for relative in _tracked_product_paths(root):
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            blob_id = _git(root, "hash-object", "--path", relative, "--", relative)
            digest.update(blob_id.encode("ascii"))
        else:
            digest.update(b"<deleted>")
        digest.update(b"\0")
    return digest.hexdigest()


def classify_product_progress(paths: tuple[str, ...]) -> tuple[str, ...]:
    progress: set[str] = set()
    for path in paths:
        if path.startswith("frontend/src/") or path.startswith("miniapp/src/"):
            progress.add("UI")
        if path.startswith("packages/trip-check-client/src/"):
            progress.add("API")
        if path.startswith("backend/app/"):
            progress.add("API" if "/api/" in f"/{path}" else "RUNTIME")
        if path in PRODUCT_CONFIG_PATHS:
            progress.add("RUNTIME")
    return tuple(sorted(progress))


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _is_governance(path: str) -> bool:
    return path in GOVERNANCE_EXACT or path.startswith(GOVERNANCE_PREFIXES)


def _validate_contract(contract: dict[str, Any]) -> None:
    if contract.get("schema_version") != "product-delivery-gates-v1":
        raise CoreMainlineError("unsupported product delivery contract version")
    if contract.get("program_id") != "TC-VNEXT-2026":
        raise CoreMainlineError("product delivery contract uses the wrong Program")
    goals = contract.get("goals")
    if not isinstance(goals, list) or len(goals) != 7:
        raise CoreMainlineError("product delivery contract must define exactly G01-G07")
    sequences = [item.get("goal_sequence") for item in goals if isinstance(item, dict)]
    if sequences != list(range(1, 8)):
        raise CoreMainlineError("product delivery goals must be ordered G01 through G07")
    for item in goals:
        expected = "HARDENED_CANDIDATE_GATE" if item["goal_sequence"] == 7 else "PRODUCT_DELIVERY_GATE"
        if item.get("gate_profile") != expected:
            raise CoreMainlineError(f"G{item['goal_sequence']:02d} uses the wrong gate profile")
    if contract.get("g03_completion_state") != "CORE_MVP_OWNER_REVIEW_PENDING":
        raise CoreMainlineError("G03 must stop at owner experience review")
    if contract.get("max_repair_review_cycles") != 2:
        raise CoreMainlineError("repair-review budget must remain exactly two cycles")


def _validate_active_files(
    root: Path,
    contract: dict[str, Any],
    binding: dict[str, Any],
    registry: dict[str, Any],
) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
    if binding.get("schema_version") != "current-goal-binding-v3":
        raise CoreMainlineError("active Goal binding must use current-goal-binding-v3")
    if registry.get("schema_version") != "work-package-registry-v3":
        raise CoreMainlineError("active work packages must use work-package-registry-v3")
    sequence = binding.get("goal_sequence")
    if not isinstance(sequence, int) or not 1 <= sequence <= 7:
        raise CoreMainlineError("active Goal sequence is invalid")
    owner_review_hold = binding.get("program_state") == OWNER_REVIEW_STATE
    if owner_review_hold and sequence != 3:
        raise CoreMainlineError("owner review hold must remain bound to completed G03")
    goal_contract = contract["goals"][sequence - 1]
    expected_goal_id = OWNER_REVIEW_STATE if owner_review_hold else goal_contract.get("goal_id")
    facts = (
        (binding.get("goal_id"), expected_goal_id, "Goal ID"),
        (binding.get("gate_profile"), goal_contract.get("gate_profile"), "Gate profile"),
        (registry.get("active_goal_id"), binding.get("goal_id"), "registry Goal"),
        (registry.get("active_goal_sequence"), sequence, "registry sequence"),
        (registry.get("gate_profile"), binding.get("gate_profile"), "registry profile"),
        (registry.get("mainline_phase"), binding.get("mainline_phase"), "mainline phase"),
    )
    for observed, expected, label in facts:
        if observed != expected:
            raise CoreMainlineError(f"{label} binding disagrees")
    contract_path = binding.get("automated_gate_contract_path")
    if contract_path != CONTRACT_PATH:
        raise CoreMainlineError("active Goal does not bind the product delivery contract")
    if binding.get("automated_gate_contract_sha256") != _sha256(root / CONTRACT_PATH):
        raise CoreMainlineError("product delivery contract hash binding differs")
    if registry.get("guidance_sha256") != _sha256(root / GUIDANCE_PATH):
        raise CoreMainlineError("work package registry does not bind current AGENTS.md")
    goal_state: dict[str, Any] | None = None
    if sequence <= 6:
        current_text = (root / CURRENT_GOAL_PATH).read_text(encoding="utf-8")
        matches = list(_PRODUCT_GOAL_STATE_PATTERN.finditer(current_text))
        if len(matches) != 1:
            raise CoreMainlineError(
                "CURRENT_GOAL.md must contain one product delivery machine state"
            )
        try:
            goal_state = json.loads(matches[0].group("payload"))
        except json.JSONDecodeError as exc:
            raise CoreMainlineError(
                "CURRENT_GOAL.md product delivery state is invalid JSON"
            ) from exc
        state_facts = (
            (
                goal_state.get("schema_version"),
                "product-delivery-current-goal-state-v1",
                "document schema",
            ),
            (goal_state.get("program_id"), "TC-VNEXT-2026", "document Program"),
            (goal_state.get("goal_id"), binding.get("goal_id"), "document Goal"),
            (goal_state.get("goal_status"), binding.get("status"), "document status"),
            (
                goal_state.get("gate_profile"),
                binding.get("gate_profile"),
                "document gate profile",
            ),
        )
        for observed, expected, label in state_facts:
            if observed != expected:
                raise CoreMainlineError(f"{label} binding disagrees")
        gate_result = goal_state.get("gate_result")
        completion_status = goal_state.get("completion_status")
        if owner_review_hold:
            terminal_facts = (
                (binding.get("status"), OWNER_REVIEW_STATUS, "owner review binding status"),
                (binding.get("last_completed_goal_id"), G03_GOAL_ID, "last completed Goal"),
                (binding.get("next_goal_id"), G04_GOAL_ID, "owner review next Goal"),
                (binding.get("next_goal_status"), "NOT_ACTIVATED", "next Goal status"),
                (registry.get("program_state"), OWNER_REVIEW_STATE, "registry program state"),
                (registry.get("writer_activation"), "NONE", "writer activation"),
                (registry.get("next_goal_id"), G04_GOAL_ID, "registry next Goal"),
                (registry.get("next_goal_status"), "NOT_ACTIVATED", "registry next status"),
                (gate_result, "PRODUCT_DELIVERY_PASS", "owner review gate result"),
                (completion_status, "DELIVERY_INTEGRATED", "owner review completion"),
                (goal_state.get("goal_archived"), True, "G03 archive state"),
                (goal_state.get("last_completed_goal_id"), G03_GOAL_ID, "document completed Goal"),
                (goal_state.get("next_goal_id"), G04_GOAL_ID, "document next Goal"),
                (goal_state.get("next_activated"), False, "document next activation"),
                (goal_state.get("g04_status"), "NOT_ACTIVATED", "G04 status"),
                (goal_state.get("fux03_status"), "NOT_RUN", "FUX-03 status"),
                (goal_state.get("h1_status"), "NOT_RUN", "H1 status"),
                (goal_state.get("public_network_status"), "NOT_RUN", "public network status"),
                (goal_state.get("production_status"), "NOT_RUN", "production status"),
                (goal_state.get("commercial_status"), "NOT_RUN", "commercial status"),
                (goal_state.get("release_status"), "NOT_REQUESTED", "release status"),
                (goal_state.get("deployment_status"), "NOT_REQUESTED", "deployment status"),
                (goal_state.get("main_merge_status"), "NOT_REQUESTED", "main merge status"),
            )
            for observed, expected, label in terminal_facts:
                if observed != expected:
                    raise CoreMainlineError(f"{label} disagrees with owner review hold")
        else:
            valid_states = {
                "PRODUCT_DELIVERY_NOT_RUN": "PENDING",
                "PRODUCT_DELIVERY_PASS": "DELIVERY_VERIFIED_PENDING_INTEGRATION",
            }
            if valid_states.get(gate_result) != completion_status:
                raise CoreMainlineError("product delivery document state is inconsistent")
        required_gate = str(goal_state.get("required_gate", ""))
        if "PRODUCT_DELIVERY_PASS" not in required_gate:
            raise CoreMainlineError("current Goal does not require product delivery PASS")
        visible_facts = (
            f"Goal ID: {binding.get('goal_id')}",
            f"Status: {binding.get('status')}",
            f"- Gate profile：`{binding.get('gate_profile')}`",
            f"- Required gate：`{required_gate}`",
        )
        if any(fact not in current_text for fact in visible_facts):
            raise CoreMainlineError("CURRENT_GOAL.md visible state disagrees")
    active = registry.get("active_slice")
    if not isinstance(active, dict):
        raise CoreMainlineError("active work package slice is missing")
    if registry.get("scope_guard_version") != "core-mainline-v1":
        raise CoreMainlineError("active registry does not use core-mainline-v1")
    if owner_review_hold:
        terminal_slice = (
            (active.get("work_kind"), "GOAL_TRANSITION", "owner review work kind"),
            (active.get("phase"), "GOAL_TRANSITION", "owner review phase"),
            (active.get("product_progress"), "NONE", "owner review product progress"),
        )
        for observed, expected, label in terminal_slice:
            if observed != expected:
                raise CoreMainlineError(f"{label} is invalid")
        packages = registry.get("packages")
        if not isinstance(packages, list) or len(packages) != 1:
            raise CoreMainlineError("owner review hold must retain exactly one completed G03 package")
        package = packages[0]
        package_facts = (
            (package.get("goal_id"), G03_GOAL_ID, "owner review package Goal"),
            (package.get("role"), "INTEGRATOR", "owner review package role"),
            (package.get("status"), "MERGED", "owner review package status"),
        )
        for observed, expected, label in package_facts:
            if observed != expected:
                raise CoreMainlineError(f"{label} is invalid")
        for field in ("ready_commit", "merged_commit"):
            value = package.get(field)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
                raise CoreMainlineError(f"owner review package {field} is invalid")
    if sequence <= 3:
        allowed = {"PRODUCT", "BLOCKING_DEFECT", "GOAL_TRANSITION"}
        if active.get("work_kind") not in allowed:
            raise CoreMainlineError("G01-G03 work kind is not product-mainline work")
        if active.get("phase") not in {"IMPLEMENTING", "DELIVERY_VERIFY", "GOAL_TRANSITION"}:
            raise CoreMainlineError("G01-G03 slice uses a candidate-evidence phase")
        cycle = active.get("repair_review_cycle")
        if not isinstance(cycle, int) or not 0 <= cycle <= 2:
            raise CoreMainlineError("repair-review cycle exceeds the two-cycle budget")
        if active.get("work_kind") == "BLOCKING_DEFECT":
            issue = active.get("blocking_issue")
            if not isinstance(issue, dict) or issue.get("severity") not in {"P0", "P1"}:
                raise CoreMainlineError("BLOCKING_DEFECT requires a registered P0/P1")
            if not all(str(issue.get(key, "")).strip() for key in ("reproduction", "impact_chain", "minimum_fix")):
                raise CoreMainlineError("BLOCKING_DEFECT lacks reproduction, impact or minimum fix")
    return sequence, active, goal_state


def validate_core_mainline(
    root: Path,
    *,
    base_ref: str,
    head_ref: str = "HEAD",
) -> CoreMainlineReport:
    root = root.resolve()
    contract = _read_json(root, CONTRACT_PATH)
    binding = _read_json(root, BINDING_PATH)
    registry = _read_json(root, REGISTRY_PATH)
    _validate_contract(contract)
    sequence, active, goal_state = _validate_active_files(
        root,
        contract,
        binding,
        registry,
    )
    if goal_state and goal_state.get("gate_result") == "PRODUCT_DELIVERY_PASS":
        validate_delivery_receipt(root, sequence)
    paths = changed_paths(root, base_ref, head_ref)
    progress = classify_product_progress(paths)
    base_has_contract = _git_object_exists(root, f"{base_ref}:{CONTRACT_PATH}")
    bootstrap = not base_has_contract
    base_binding = _read_json_at_ref(root, base_ref, BINDING_PATH)
    base_sequence = base_binding.get("goal_sequence") if base_binding else None
    owner_review_transition = (
        binding.get("program_state") == OWNER_REVIEW_STATE
        and base_sequence == 3
        and base_binding is not None
        and base_binding.get("goal_id") == G03_GOAL_ID
    )
    blocking_issue = active.get("blocking_issue")
    owner_review_repair_transition = (
        base_binding is not None
        and base_binding.get("program_state") == OWNER_REVIEW_STATE
        and base_binding.get("goal_id") == OWNER_REVIEW_STATE
        and base_sequence == 3
        and sequence == 3
        and binding.get("goal_id") == G03_GOAL_ID
        and active.get("work_kind") == "BLOCKING_DEFECT"
        and isinstance(blocking_issue, dict)
        and blocking_issue.get("severity") == "P1"
        and blocking_issue.get("current_goal_acceptance_ref")
        == G03_REPAIR_OWNER_AUTHORIZATION
    )
    goal_transition = (
        base_has_contract
        and isinstance(base_sequence, int)
        and (
            sequence == base_sequence + 1
            or owner_review_transition
            or owner_review_repair_transition
        )
    )
    errors: list[str] = []
    if (
        sequence == 4
        and registry.get("active_goal_id") == G04_GOAL_ID
        and registry.get("schema_version") == "work-package-registry-v3"
        and registry.get("packages")
    ):
        from governance.work_packages_v3 import validate_registry_v3

        work_packages = validate_registry_v3(root)
        if work_packages["verdict"] != "PASS":
            errors.extend(
                f"WORK_PACKAGE_V3_{code}"
                for code in work_packages["error_codes"]
            )

    if (
        base_has_contract
        and isinstance(base_sequence, int)
        and sequence != base_sequence
        and not goal_transition
    ):
        errors.append("INVALID_GOAL_SEQUENCE_TRANSITION")

    frozen_patterns = contract.get("frozen_g07_asset_patterns", [])
    deferred_patterns = contract.get("deferred_work_patterns", [])
    if not all(isinstance(item, str) and item for item in (*frozen_patterns, *deferred_patterns)):
        raise CoreMainlineError("delivery contract path patterns are invalid")
    repair_path_exemptions = (
        {G03_REPAIR_SEMANTIC_PROMPT_PATH}
        if owner_review_repair_transition
        and G03_REPAIR_SEMANTIC_PROMPT_PATH in active.get("allowed_paths", [])
        else set()
    )
    frozen_changes = tuple(
        path
        for path in paths
        if _matches_any(path, frozen_patterns) and path not in repair_path_exemptions
    )
    deferred_changes = tuple(
        path
        for path in paths
        if _matches_any(path, deferred_patterns) and path not in repair_path_exemptions
    )

    if sequence <= 6 and not bootstrap:
        if frozen_changes:
            errors.append("FROZEN_G07_ASSET_CHANGED")
    if sequence <= 3 and not bootstrap:
        if deferred_changes:
            errors.append("DEFERRED_DETAIL_WORK_CHANGED")

    declared_work_kind = str(active.get("work_kind"))
    work_kind = (
        declared_work_kind
        if owner_review_repair_transition and progress
        else "GOAL_TRANSITION"
        if goal_transition
        else declared_work_kind
    )
    if sequence <= 3 and work_kind in {"PRODUCT", "BLOCKING_DEFECT"} and not progress:
        errors.append("PRODUCT_PROGRESS_NONE")
    if sequence <= 3 and paths and all(_is_governance(path) or path.startswith("backend/tests/") for path in paths):
        if work_kind != "GOAL_TRANSITION" and not bootstrap:
            errors.append("GOVERNANCE_ONLY_SLICE")
    if work_kind == "GOAL_TRANSITION":
        illegal = [
            path
            for path in paths
            if not _is_governance(path)
            and not path.startswith("backend/governance/")
            and path != "backend/scripts/validate_core_mainline.py"
            and not path.startswith("backend/tests/test_product_delivery_")
            and path != "backend/tests/test_governance_agent_gate_transition.py"
        ]
        if illegal:
            errors.append("GOAL_TRANSITION_CHANGED_PRODUCT")

    declared_progress = active.get("product_progress")
    if declared_progress == "NONE" and progress and work_kind != "GOAL_TRANSITION":
        errors.append("DECLARED_PRODUCT_PROGRESS_FALSE")
    if declared_progress != "NONE" and not progress and work_kind in {"PRODUCT", "BLOCKING_DEFECT"}:
        errors.append("DECLARED_PRODUCT_PROGRESS_UNSUPPORTED")

    protected_patterns = contract.get("owner_protected_patterns", [])
    owner_review = any(_matches_any(path, protected_patterns) for path in paths)
    if bootstrap:
        expected_approval = "OWNER_APPROVED_MAINLINE_CORRECTION_2026-08-29"
        if contract.get("bootstrap_owner_approval") != expected_approval:
            errors.append("BOOTSTRAP_OWNER_APPROVAL_MISSING")

    return CoreMainlineReport(
        verdict="PASS" if not errors else "FAIL",
        goal_id=str(binding.get("goal_id")),
        goal_sequence=sequence,
        delivery_goal_sequence=(base_sequence if goal_transition else sequence),
        work_kind=work_kind,
        phase=str(active.get("phase")),
        changed_files=paths,
        product_progress=progress,
        product_fingerprint=product_fingerprint(root),
        frozen_g07_changes=frozen_changes,
        deferred_work_changes=deferred_changes,
        owner_review_required=owner_review,
        bootstrap_correction=bootstrap,
        errors=tuple(errors),
    )


def validate_delivery_receipt(root: Path, goal_sequence: int) -> dict[str, Any]:
    contract = _read_json(root, CONTRACT_PATH)
    _validate_contract(contract)
    goal = contract["goals"][goal_sequence - 1]
    receipt_path = f"docs/governance/gate-results/G{goal_sequence:02d}.product-delivery.json"
    receipt = _read_json(root, receipt_path)
    if receipt.get("schema_version") != "product-delivery-result-v1":
        raise CoreMainlineError("delivery result uses the wrong schema")
    if receipt.get("goal_id") != goal["goal_id"] or receipt.get("gate_profile") != goal["gate_profile"]:
        raise CoreMainlineError("delivery result binds the wrong Goal")
    recorded_fingerprint = receipt.get("product_fingerprint")
    if not isinstance(recorded_fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", recorded_fingerprint) is None:
        raise CoreMainlineError("delivery result product fingerprint is invalid")
    binding = _read_json(root, BINDING_PATH)
    active_sequence = binding.get("goal_sequence")
    if (
        not isinstance(active_sequence, int)
        or goal_sequence >= active_sequence
    ) and recorded_fingerprint != product_fingerprint(root):
        raise CoreMainlineError("delivery result is stale for current product/runtime bytes")
    required = set(goal.get("required_checks", []))
    checks = receipt.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) != "PASS" for name in required):
        raise CoreMainlineError("current Goal product delivery checks are incomplete")
    if receipt.get("verdict") != "PASS":
        raise CoreMainlineError("current Goal delivery result is not PASS")
    if goal_sequence == 4:
        from governance.g04_screenshot_parity import (
            G04ParityReceiptError,
            validate_g04_delivery_evidence,
        )

        current_fingerprint = (
            product_fingerprint(root)
            if not isinstance(active_sequence, int) or active_sequence <= goal_sequence
            else None
        )
        try:
            validate_g04_delivery_evidence(
                root,
                receipt,
                expected_product_fingerprint=recorded_fingerprint,
                current_product_fingerprint=current_fingerprint,
            )
        except G04ParityReceiptError as exc:
            raise CoreMainlineError(f"G04 formal parity evidence is invalid: {exc}") from exc
    # G07 evidence is deliberately absent from G01-G06 receipts and is never consulted here.
    return receipt
