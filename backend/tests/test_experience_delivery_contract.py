from __future__ import annotations

import json
from pathlib import Path

import pytest

from governance.experience_delivery import (
    BINDING_PATH,
    CONTRACT_PATH,
    REGISTRY_PATH,
    uses_experience_delivery,
    validate_experience_delivery,
)


SAFETY_IDS = {
    "PLACE_IDENTITY", "PRIVATE_DATA_PROTECTION", "DELETION_COMPLETENESS",
    "EDIT_NO_AUTOMATIC_ROUTING", "UNKNOWN_NOT_SUCCESS", "NO_FABRICATED_ROUTE", "REVISION_CONSISTENCY",
}


def _write(root: Path, path: str, value: dict) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def contract_root(tmp_path: Path) -> Path:
    for path in ("AGENTS.md", "docs/governance/CURRENT_GOAL.md", "docs/product/PROJECT_CHARTER.md", "docs/governance/IMPLEMENTATION_PLAN.md"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Product direction\n", encoding="utf-8")
    _write(tmp_path, BINDING_PATH, {
        "schema_version": "experience-goal-v1", "goal_id": "TC-EXPERIENCE-V1",
        "status": "IN_PROGRESS", "stage": 0, "gate_profile": "EXPERIENCE_DELIVERY",
        "product_contract_path": "docs/product/PROJECT_CHARTER.md",
        "implementation_plan_path": "docs/governance/IMPLEMENTATION_PLAN.md",
        "automated_gate_contract_path": CONTRACT_PATH,
    })
    _write(tmp_path, REGISTRY_PATH, {
        "schema_version": "experience-work-packages-v1", "active_goal_id": "TC-EXPERIENCE-V1",
        "active_slice": {"id": "direction-switch", "work_kind": "DIRECTION_TRANSITION", "outcome": "One product direction"},
        "packages": [],
    })
    _write(tmp_path, CONTRACT_PATH, {
        "schema_version": "experience-delivery-v1", "goal_id": "TC-EXPERIENCE-V1",
        "deep_cities": ["北京", "上海", "杭州"],
        "core_scenarios": [
            {"id": f"{city}-{scenario}", "city": city, "scenario": scenario}
            for city in ("北京", "上海", "杭州")
            for scenario in ("normal", "ambiguous", "schedule_conflict", "edit_recovery")
        ],
        "required_checks": ["contract", "backend", "postgres", "frontend", "browser"],
        "safety_invariants": sorted(SAFETY_IDS), "deferred": [],
    })
    return tmp_path


def _change(root: Path, path: str, key: str, value: object) -> None:
    document = json.loads((root / path).read_text(encoding="utf-8"))
    document[key] = value
    _write(root, path, document)


def test_contract_pass_does_not_claim_product_or_browser_delivery(contract_root: Path) -> None:
    assert uses_experience_delivery(contract_root)
    result = validate_experience_delivery(contract_root)
    assert result["verdict"] == "PASS", result
    assert result["validation_scope"] == "CONTRACT_ONLY"
    assert result["product_delivery"] == "NOT_RUN"


@pytest.mark.parametrize("path,key,value", [
    (REGISTRY_PATH, "active_goal_id", "another-goal"),
    (CONTRACT_PATH, "goal_id", "another-goal"),
    (BINDING_PATH, "schema_version", "current-goal-binding-v3"),
    (REGISTRY_PATH, "schema_version", "work-package-registry-v3"),
    (BINDING_PATH, "stage", True),
    (BINDING_PATH, "stage", -1),
    (BINDING_PATH, "product_contract_path", "../outside.md"),
    (BINDING_PATH, "implementation_plan_path", "missing.md"),
    (CONTRACT_PATH, "deep_cities", ["北京", "上海", "上海"]),
    (CONTRACT_PATH, "required_checks", ["contract"]),
    (REGISTRY_PATH, "active_slice", {}),
])
def test_inconsistent_or_incomplete_direction_fails(contract_root: Path, path: str, key: str, value: object) -> None:
    _change(contract_root, path, key, value)
    assert validate_experience_delivery(contract_root)["verdict"] == "FAIL"


@pytest.mark.parametrize("missing", sorted(SAFETY_IDS))
def test_each_safety_boundary_must_remain(contract_root: Path, missing: str) -> None:
    _change(contract_root, CONTRACT_PATH, "safety_invariants", sorted(SAFETY_IDS - {missing}))
    assert validate_experience_delivery(contract_root)["verdict"] == "FAIL"


def test_twelve_rows_cannot_hide_a_missing_city_scenario(contract_root: Path) -> None:
    document = json.loads((contract_root / CONTRACT_PATH).read_text(encoding="utf-8"))
    document["core_scenarios"][-1] = {**document["core_scenarios"][0], "id": "different-id"}
    _write(contract_root, CONTRACT_PATH, document)
    assert validate_experience_delivery(contract_root)["verdict"] == "FAIL"


def test_duplicate_scenario_ids_fail(contract_root: Path) -> None:
    document = json.loads((contract_root / CONTRACT_PATH).read_text(encoding="utf-8"))
    document["core_scenarios"][-1]["id"] = document["core_scenarios"][0]["id"]
    _write(contract_root, CONTRACT_PATH, document)
    assert validate_experience_delivery(contract_root)["verdict"] == "FAIL"


def test_invalid_json_is_a_contract_failure(contract_root: Path) -> None:
    (contract_root / CONTRACT_PATH).write_text("not JSON", encoding="utf-8")
    assert validate_experience_delivery(contract_root)["verdict"] == "FAIL"


def test_legacy_schemas_keep_the_legacy_route(contract_root: Path) -> None:
    _change(contract_root, BINDING_PATH, "schema_version", "current-goal-binding-v3")
    _change(contract_root, REGISTRY_PATH, "schema_version", "work-package-registry-v3")
    assert not uses_experience_delivery(contract_root)


@pytest.mark.parametrize("module_name", ["scripts.validate_core_mainline", "scripts.validate_work_packages"])
def test_cli_uses_current_contract_without_git_hashes(contract_root: Path, monkeypatch, capsys, module_name: str) -> None:
    from importlib import import_module

    module = import_module(module_name)
    monkeypatch.setattr(module, "REPOSITORY_ROOT", contract_root)
    monkeypatch.setattr("sys.argv", [module_name])
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["product_delivery"] == "NOT_RUN"


def test_cli_cannot_turn_contract_validation_into_delivery_pass(contract_root: Path, monkeypatch, capsys) -> None:
    from scripts import validate_core_mainline

    monkeypatch.setattr(validate_core_mainline, "REPOSITORY_ROOT", contract_root)
    monkeypatch.setattr("sys.argv", ["validate_core_mainline", "--require-delivery-pass"])
    assert validate_core_mainline.main() == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "FAIL"
