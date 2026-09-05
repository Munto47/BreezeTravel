"""Small consistency checks for the current product direction, not a delivery verdict."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BINDING_PATH = "docs/governance/current_goal_binding.json"
REGISTRY_PATH = "docs/governance/current_work_packages.json"
CONTRACT_PATH = "docs/governance/product_delivery_gates.json"
SCENARIOS = {"normal", "ambiguous", "schedule_conflict", "edit_recovery"}
DEEP_CITIES = {"北京", "上海", "杭州"}
REQUIRED_CHECKS = {"contract", "backend", "postgres", "frontend", "browser"}
REQUIRED_SAFETY_INVARIANTS = {
    "PLACE_IDENTITY",
    "PRIVATE_DATA_PROTECTION",
    "DELETION_COMPLETENESS",
    "EDIT_NO_AUTOMATIC_ROUTING",
    "UNKNOWN_NOT_SUCCESS",
    "NO_FABRICATED_ROUTE",
    "REVISION_CONSISTENCY",
}


class ExperienceContractError(ValueError):
    pass


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads((root / relative).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperienceContractError(f"Cannot read JSON contract: {relative}") from exc
    if not isinstance(value, dict):
        raise ExperienceContractError(f"JSON contract must be an object: {relative}")
    return value


def uses_experience_delivery(root: Path) -> bool:
    return any(
        _read_json(root, path).get("schema_version") == schema
        for path, schema in (
            (BINDING_PATH, "experience-goal-v1"),
            (REGISTRY_PATH, "experience-work-packages-v1"),
        )
    )


def _document_exists(root: Path, relative: Any) -> bool:
    if not isinstance(relative, str) or not relative.strip():
        return False
    path = (root / relative).resolve()
    return path.is_relative_to(root.resolve()) and path.is_file()


def validate_experience_delivery(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        binding = _read_json(root, BINDING_PATH)
        registry = _read_json(root, REGISTRY_PATH)
        contract = _read_json(root, CONTRACT_PATH)
    except ExperienceContractError as exc:
        return {
            "verdict": "FAIL", "validation_scope": "CONTRACT_ONLY",
            "product_delivery": "NOT_RUN", "errors": [str(exc)],
        }

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(binding.get("schema_version") == "experience-goal-v1", "Goal schema mismatch")
    require(registry.get("schema_version") == "experience-work-packages-v1", "Work packages schema mismatch")
    require(contract.get("schema_version") == "experience-delivery-v1", "Delivery schema mismatch")
    goal_id = binding.get("goal_id")
    require(isinstance(goal_id, str) and bool(goal_id.strip()), "Goal id is required")
    require(goal_id == registry.get("active_goal_id") == contract.get("goal_id"), "Goal ids disagree")
    require(binding.get("status") in {"IN_PROGRESS", "COMPLETED"}, "Goal status is invalid")
    stage = binding.get("stage")
    require(type(stage) is int and stage >= 0, "Stage must be a non-negative integer")
    require(binding.get("gate_profile") == "EXPERIENCE_DELIVERY", "Gate profile mismatch")
    require(binding.get("automated_gate_contract_path") == CONTRACT_PATH, "Delivery contract path mismatch")
    for key in ("product_contract_path", "implementation_plan_path"):
        require(_document_exists(root, binding.get(key)), f"Missing repository document: {key}")
    for path in ("AGENTS.md", "docs/governance/CURRENT_GOAL.md"):
        require(_document_exists(root, path), f"Missing repository document: {path}")

    active = registry.get("active_slice")
    require(isinstance(active, dict), "Active slice is required")
    if isinstance(active, dict):
        for key in ("id", "work_kind", "outcome"):
            require(isinstance(active.get(key), str) and bool(active[key].strip()), f"Active slice needs {key}")
    require(isinstance(registry.get("packages"), list), "Packages must be a list")
    cities = contract.get("deep_cities")
    require(isinstance(cities, list) and len(cities) == 3 and all(isinstance(city, str) for city in cities)
            and set(cities) == DEEP_CITIES, "Deep cities must be Beijing, Shanghai and Hangzhou")
    scenarios = contract.get("core_scenarios")
    require(isinstance(scenarios, list) and len(scenarios) == 12, "Exactly 12 core scenarios are required")
    pairs: list[tuple[str, str]] = []
    ids: list[str] = []
    if isinstance(scenarios, list):
        for item in scenarios:
            if not isinstance(item, dict):
                errors.append("Each core scenario must be an object")
                continue
            identifier, city, scenario = item.get("id"), item.get("city"), item.get("scenario")
            require(isinstance(identifier, str) and bool(identifier.strip()), "Each scenario needs an id")
            if isinstance(identifier, str):
                ids.append(identifier)
            if isinstance(city, str) and isinstance(scenario, str):
                pairs.append((city, scenario))
    require(len(ids) == len(set(ids)) == 12, "Scenario ids must be unique")
    require(len(pairs) == 12 and set(pairs) == {(city, scenario) for city in DEEP_CITIES for scenario in SCENARIOS},
            "Each deep city needs normal, ambiguous, schedule_conflict and edit_recovery coverage")
    for key, required in (("required_checks", REQUIRED_CHECKS), ("safety_invariants", REQUIRED_SAFETY_INVARIANTS)):
        values = contract.get(key)
        require(isinstance(values, list) and all(isinstance(value, str) for value in values)
                and required.issubset(values), f"Missing required {key}")
    require(isinstance(contract.get("deferred"), list), "Deferred work must be listed")
    return {
        "verdict": "FAIL" if errors else "PASS",
        "validation_scope": "CONTRACT_ONLY",
        "product_delivery": "NOT_RUN",
        "goal_id": goal_id,
        "stage": stage,
        "core_scenario_count": len(ids),
        "errors": errors,
    }
