from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from governance.core_mainline import (
    CONTRACT_PATH,
    CoreMainlineError,
    product_fingerprint,
    validate_core_mainline,
    validate_delivery_receipt,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _delivery_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "backend/app").mkdir(parents=True)
    (root / "docs/governance").mkdir(parents=True)
    (root / "backend/app/main.py").write_text("USER_FLOW = 'cards'\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("product mainline\n", encoding="utf-8")
    contract = json.loads((REPOSITORY_ROOT / CONTRACT_PATH).read_text(encoding="utf-8"))
    _write_json(root / CONTRACT_PATH, contract)
    contract_hash = hashlib.sha256((root / CONTRACT_PATH).read_bytes()).hexdigest()
    guidance_hash = hashlib.sha256((root / "AGENTS.md").read_bytes()).hexdigest()
    _write_json(
        root / "docs/governance/current_goal_binding.json",
        {
            "schema_version": "current-goal-binding-v3",
            "program_id": "TC-VNEXT-2026",
            "goal_sequence": 1,
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "status": "IN_PROGRESS",
            "gate_profile": "PRODUCT_DELIVERY_GATE",
            "mainline_phase": "CORE_MVP",
            "automated_gate_contract_path": CONTRACT_PATH,
            "automated_gate_contract_sha256": contract_hash,
        },
    )
    _write_json(
        root / "docs/governance/current_work_packages.json",
        {
            "schema_version": "work-package-registry-v3",
            "active_goal_sequence": 1,
            "active_goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "gate_profile": "PRODUCT_DELIVERY_GATE",
            "mainline_phase": "CORE_MVP",
            "guidance_sha256": guidance_hash,
            "scope_guard_version": "core-mainline-v1",
            "active_slice": {
                "work_kind": "PRODUCT",
                "phase": "IMPLEMENTING",
                "repair_review_cycle": 0,
                "product_progress": "RUNTIME",
            },
        },
    )
    (root / "docs/governance/CURRENT_GOAL.md").write_text(
        "# IN_PROGRESS GOAL：G01\n\n"
        "Goal ID: TC-VNEXT-G01-TEXT-CARDS\n"
        "Status: IN_PROGRESS\n\n"
        "<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE\n"
        "{\n"
        '  "schema_version": "product-delivery-current-goal-state-v1",\n'
        '  "program_id": "TC-VNEXT-2026",\n'
        '  "goal_id": "TC-VNEXT-G01-TEXT-CARDS",\n'
        '  "goal_status": "IN_PROGRESS",\n'
        '  "gate_profile": "PRODUCT_DELIVERY_GATE",\n'
        '  "required_gate": "Text Card Gate + PRODUCT_DELIVERY_PASS",\n'
        '  "completion_status": "PENDING",\n'
        '  "gate_result": "PRODUCT_DELIVERY_NOT_RUN"\n'
        "}\n"
        "-->\n\n"
        "- Gate profile：`PRODUCT_DELIVERY_GATE`\n"
        "- Required gate：`Text Card Gate + PRODUCT_DELIVERY_PASS`\n",
        encoding="utf-8",
    )
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Core Mainline Test")
    return root, _commit(root, "install product delivery gate")


def test_checked_in_contract_separates_delivery_from_candidate_hardening() -> None:
    contract = json.loads((REPOSITORY_ROOT / CONTRACT_PATH).read_text(encoding="utf-8"))
    assert [item["gate_profile"] for item in contract["goals"]] == [
        *("PRODUCT_DELIVERY_GATE" for _ in range(6)),
        "HARDENED_CANDIDATE_GATE",
    ]
    assert contract["g03_completion_state"] == "CORE_MVP_OWNER_REVIEW_PENDING"
    assert contract["max_repair_review_cycles"] == 2
    assert contract["goals"][0]["fixed_case_ids"] == [
        "G01-TC-001",
        "G01-TC-013",
        "G01-TC-025",
        "G01-TC-037",
        "G01-TC-046",
    ]
    assert validate_delivery_receipt(REPOSITORY_ROOT, 1)["verdict"] == "PASS"


def test_product_runtime_change_passes_current_delivery_scope(tmp_path: Path) -> None:
    root, base = _delivery_repo(tmp_path)
    (root / "backend/app/main.py").write_text("USER_FLOW = 'editable-cards'\n", encoding="utf-8")
    _commit(root, "deliver cards")

    report = validate_core_mainline(root, base_ref=base)

    assert report.verdict == "PASS"
    assert report.product_progress == ("RUNTIME",)


def test_product_progress_none_cannot_pass_as_product_work(tmp_path: Path) -> None:
    root, base = _delivery_repo(tmp_path)
    goal_path = root / "docs/governance/CURRENT_GOAL.md"
    goal_path.write_text(
        goal_path.read_text(encoding="utf-8") + "\nmore governance\n",
        encoding="utf-8",
    )
    _commit(root, "docs only")

    report = validate_core_mainline(root, base_ref=base)

    assert report.verdict == "FAIL"
    assert "PRODUCT_PROGRESS_NONE" in report.errors
    assert "GOVERNANCE_ONLY_SLICE" in report.errors


def test_g01_candidate_assets_are_machine_rejected_after_bootstrap(tmp_path: Path) -> None:
    root, base = _delivery_repo(tmp_path)
    candidate = root / "backend/evals/agent_gate_v1/authority.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("CANDIDATE = True\n", encoding="utf-8")
    _commit(root, "attempt candidate infrastructure")

    report = validate_core_mainline(root, base_ref=base)

    assert report.verdict == "FAIL"
    assert "FROZEN_G07_ASSET_CHANGED" in report.errors
    assert "DEFERRED_DETAIL_WORK_CHANGED" in report.errors


def test_docs_and_gate_scripts_do_not_invalidate_product_fingerprint(tmp_path: Path) -> None:
    root, _base = _delivery_repo(tmp_path)
    before = product_fingerprint(root)
    (root / "docs/governance/CURRENT_GOAL.md").write_text("one-line checkpoint\n", encoding="utf-8")
    script = root / "backend/scripts/verify_note.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('note')\n", encoding="utf-8")

    assert product_fingerprint(root) == before

    (root / "backend/app/main.py").write_text("USER_FLOW = 'changed'\n", encoding="utf-8")
    assert product_fingerprint(root) != before


def test_delivery_receipt_ignores_not_run_g07_items(tmp_path: Path) -> None:
    root, _base = _delivery_repo(tmp_path)
    fingerprint = product_fingerprint(root)
    receipt = {
        "schema_version": "product-delivery-result-v1",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "gate_profile": "PRODUCT_DELIVERY_GATE",
        "product_fingerprint": fingerprint,
        "checks": {
            "core_mainline_contract": "PASS",
            "g01_fixed_samples_and_v3": "PASS",
            "g01_postgresql": "PASS",
            "frontend_build": "PASS",
            "g01_browser_e2e": "PASS",
        },
        "g07_candidate_evidence": "NOT_RUN",
        "verdict": "PASS",
    }
    _write_json(root / "docs/governance/gate-results/G01.product-delivery.json", receipt)

    assert validate_delivery_receipt(root, 1)["g07_candidate_evidence"] == "NOT_RUN"

    (root / "backend/app/main.py").write_text("USER_FLOW = 'stale'\n", encoding="utf-8")
    with pytest.raises(CoreMainlineError, match="stale"):
        validate_delivery_receipt(root, 1)


def test_atomic_g01_to_g02_transition_uses_g01_receipt_and_activates_product_work(
    tmp_path: Path,
) -> None:
    root, base = _delivery_repo(tmp_path)
    binding_path = root / "docs/governance/current_goal_binding.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding.update(
        {
            "goal_sequence": 2,
            "goal_id": "TC-VNEXT-G02-MAP-STAY",
            "mainline_phase": "CORE_MVP",
        }
    )
    _write_json(binding_path, binding)

    registry_path = root / "docs/governance/current_work_packages.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry.update(
        {
            "active_goal_sequence": 2,
            "active_goal_id": "TC-VNEXT-G02-MAP-STAY",
        }
    )
    registry["active_slice"].update(
        {
            "work_kind": "PRODUCT",
            "phase": "IMPLEMENTING",
            "product_progress": "RUNTIME",
        }
    )
    _write_json(registry_path, registry)
    (root / "docs/governance/CURRENT_GOAL.md").write_text(
        "# IN_PROGRESS GOAL：G02\n\n"
        "Goal ID: TC-VNEXT-G02-MAP-STAY\n"
        "Status: IN_PROGRESS\n\n"
        "<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE\n"
        "{\n"
        '  "schema_version": "product-delivery-current-goal-state-v1",\n'
        '  "program_id": "TC-VNEXT-2026",\n'
        '  "goal_id": "TC-VNEXT-G02-MAP-STAY",\n'
        '  "goal_status": "IN_PROGRESS",\n'
        '  "gate_profile": "PRODUCT_DELIVERY_GATE",\n'
        '  "required_gate": "Map & Stay Gate + PRODUCT_DELIVERY_PASS",\n'
        '  "completion_status": "PENDING",\n'
        '  "gate_result": "PRODUCT_DELIVERY_NOT_RUN"\n'
        "}\n"
        "-->\n\n"
        "- Gate profile：`PRODUCT_DELIVERY_GATE`\n"
        "- Required gate：`Map & Stay Gate + PRODUCT_DELIVERY_PASS`\n",
        encoding="utf-8",
    )
    _write_json(
        root / "docs/governance/gate-results/G01.product-delivery.json",
        {
            "schema_version": "product-delivery-result-v1",
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "gate_profile": "PRODUCT_DELIVERY_GATE",
            "product_fingerprint": product_fingerprint(root),
            "checks": {
                "core_mainline_contract": "PASS",
                "g01_fixed_samples_and_v3": "PASS",
                "g01_postgresql": "PASS",
                "frontend_build": "PASS",
                "g01_browser_e2e": "PASS",
            },
            "g07_candidate_evidence": "NOT_RUN",
            "verdict": "PASS",
        },
    )
    for relative in (
        "docs/product/PROJECT_CHARTER.md",
        "docs/product/TRIP_CHECK_SPEC.md",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("G03 stops for owner review.\n", encoding="utf-8")
    _commit(root, "archive G01 and activate G02")

    report = validate_core_mainline(root, base_ref=base)

    assert report.verdict == "PASS"
    assert report.work_kind == "GOAL_TRANSITION"
    assert report.product_progress == ()
    assert report.goal_sequence == 2
    assert report.delivery_goal_sequence == 1
    assert validate_delivery_receipt(root, report.delivery_goal_sequence)["verdict"] == "PASS"
