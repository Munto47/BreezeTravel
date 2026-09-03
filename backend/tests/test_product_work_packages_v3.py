from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import governance.work_packages_v3 as work_packages_v3
from governance.core_mainline import validate_delivery_receipt
from governance.work_packages_v3 import validate_registry_v3


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MACHINE_STATE_PATTERN = re.compile(
    r"<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE\n(?P<payload>\{.*?\})\n-->",
    re.DOTALL,
)


def _machine_state(document: str) -> dict[str, object]:
    match = _MACHINE_STATE_PATTERN.search(document)
    assert match is not None
    value = json.loads(match.group("payload"))
    assert isinstance(value, dict)
    return value


def test_g04_to_g06_delivery_archives_remain_verifiable_after_transition() -> None:
    g04_result = validate_delivery_receipt(REPOSITORY_ROOT, 4)
    g04_archive = (
        REPOSITORY_ROOT
        / "docs/governance/goals/completed/TC-VNEXT-G04-SCREENSHOT.md"
    ).read_text(encoding="utf-8")
    g05_result = validate_delivery_receipt(REPOSITORY_ROOT, 5)
    g05_archive = (
        REPOSITORY_ROOT
        / "docs/governance/goals/completed/TC-VNEXT-G05-CITY-KNOWLEDGE.md"
    ).read_text(encoding="utf-8")
    g06_result = validate_delivery_receipt(REPOSITORY_ROOT, 6)
    g06_archive = (
        REPOSITORY_ROOT
        / "docs/governance/goals/completed/TC-VNEXT-G06-MEMORY-SHARE.md"
    ).read_text(encoding="utf-8")

    assert g04_result["verdict"] == "PASS", g04_result
    assert g05_result["verdict"] == "PASS", g05_result
    assert g06_result["verdict"] == "PASS", g06_result
    assert g04_archive.startswith("# COMPLETED GOAL：V0.4 截图与文本一致")
    assert g05_archive.startswith("# COMPLETED GOAL：V0.5 三城有来源知识层")
    assert g06_archive.startswith("# COMPLETED GOAL：V0.6 显式记忆与分享")
    for archive in (g04_archive, g05_archive, g06_archive):
        assert '"goal_archived": true' in archive
        assert '"next_activated": true' in archive
    assert "33357640834" in g04_archive
    assert "33389553342" in g05_archive
    assert "33389970986" in g05_archive
    assert "33402192501" in g06_archive
    assert "33402780730" in g06_archive


def test_g06_archive_and_g07_active_binding_are_unambiguous() -> None:
    governance = REPOSITORY_ROOT / "docs/governance"
    current_goal = (governance / "CURRENT_GOAL.md").read_text(encoding="utf-8")
    archive_path = governance / "goals/completed/TC-VNEXT-G06-MEMORY-SHARE.md"
    archive = archive_path.read_text(encoding="utf-8")
    binding = json.loads(
        (governance / "current_goal_binding.json").read_text(encoding="utf-8")
    )
    registry = json.loads(
        (governance / "current_work_packages.json").read_text(encoding="utf-8")
    )
    current_state = _machine_state(current_goal)
    archived_state = _machine_state(archive)

    assert list((governance / "goals/planned").glob("TC-VNEXT-G07-*.md")) == []
    assert list((governance / "goals/completed").glob("TC-VNEXT-G06-*.md")) == [
        archive_path
    ]
    assert current_state == {
        "schema_version": "product-delivery-current-goal-state-v1",
        "program_id": "TC-VNEXT-2026",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "goal_status": "IN_PROGRESS",
        "gate_profile": "HARDENED_CANDIDATE_GATE",
        "required_gate": (
            "Candidate Evidence Gate G0～G7 + HARDENED_CANDIDATE_GATE_PASS"
        ),
        "completion_status": "NOT_RUN",
        "gate_result": "HARDENED_CANDIDATE_GATE_NOT_RUN",
        "goal_archived": False,
        "last_completed_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
        "next_goal_id": "TC-H1-G01-HUMAN-USABILITY",
        "next_activated": False,
        "h1_status": "NOT_RUN",
        "public_network_status": "NOT_RUN",
        "production_status": "NOT_RUN",
        "commercial_status": "NOT_RUN",
        "release_status": "NOT_REQUESTED",
        "deployment_status": "NOT_REQUESTED",
        "main_merge_status": "NOT_REQUESTED",
    }
    assert archived_state["goal_status"] == "COMPLETED"
    assert archived_state["completion_status"] == "DELIVERY_INTEGRATED"
    assert archived_state["gate_result"] == "PRODUCT_DELIVERY_PASS"
    assert archived_state["goal_archived"] is True
    assert archived_state["last_completed_goal_id"] == binding["last_completed_goal_id"]
    assert archived_state["next_goal_id"] == current_state["goal_id"]
    assert archived_state["next_activated"] is True
    assert binding["goal_id"] == registry["active_goal_id"] == current_state["goal_id"]
    assert binding["goal_sequence"] == registry["active_goal_sequence"] == 7
    assert binding["status"] == current_state["goal_status"] == "IN_PROGRESS"
    assert binding["canonical_candidate_ref"] == (
        "refs/heads/codex/g07-candidate-cycle-2"
    )
    assert binding["program_state"] == registry["program_state"]
    assert binding["predecessor_goal_id"] == archived_state["goal_id"]
    assert binding["candidate_gate_contract_path"] == (
        "backend/eval_data/agent_gate_v1/g07_automated_product_gate.json"
    )
    assert binding["candidate_gate_contract_sha256"] == (
        hashlib.sha256(
            (REPOSITORY_ROOT / binding["candidate_gate_contract_path"]).read_bytes()
        ).hexdigest()
    )
    assert registry["active_slice"]["work_kind"] == "CANDIDATE_HARDENING"
    assert registry["active_slice"]["phase"] in {
        "IMPLEMENTING",
        "PREFLIGHT",
        "EVIDENCE_FROZEN",
        "GATE_RUNNING",
    }
    assert registry["active_slice"]["candidate_cycle"] == 3
    assert registry["max_parallel_writers"] == 2
    assert [package["package_id"] for package in registry["packages"]] == [
        "WP-G07-INTEGRATOR"
    ]

    completion = archive.split("## Completion record", maxsplit=1)[1].split(
        "## Stop conditions", maxsplit=1
    )[0]
    assert "PENDING" not in completion
    assert "待提交" not in completion
    assert "待合并" not in completion

    for token in (
        "G06 Consent & Share Gate与`PRODUCT_DELIVERY_PASS`已通过并归档",
        "G04方案A两个精确历史失败例外必须在G07 exact-binding验收前移除",
        "H1、公网、生产、商业：`NOT_RUN`",
        "Next Goal activated：固定`NO_PENDING_HUMAN_APPROVAL`",
    ):
        assert token in current_goal


def test_g07_cycle_3_active_slice_remains_narrow_and_bound_to_candidate_ref() -> None:
    registry = json.loads(
        (
            REPOSITORY_ROOT / "docs/governance/current_work_packages.json"
        ).read_text(encoding="utf-8")
    )
    active_slice = registry["active_slice"]
    allowed = active_slice["allowed_paths"]
    assert active_slice["slice_id"] in {
        "G07-CANDIDATE-CONTRACT",
        "G07-COMPONENT-RAW-REVALIDATION",
        "G07-PANEL-P1-REPAIR-1",
        "G07-AUTOMATED-COMPONENT-REPAIR-2",
        "G07-SEALED-ONE-SHOT",
    }
    assert active_slice["candidate_cycle"] == 3
    assert isinstance(active_slice["repair_review_cycle"], int)
    assert active_slice["repair_review_cycle"] >= 0
    assert active_slice["work_kind"] == "CANDIDATE_HARDENING"
    assert "docs/governance" in allowed
    assert "backend/app/trip_understanding" not in allowed
    assert "frontend/src" not in allowed
    assert not any("frozen_blind" in path for path in allowed)

    integrator = registry["packages"][0]
    owned = integrator["owned_paths"]
    assert all(
        any(path == root or path.startswith(f"{root}/") for root in owned)
        for path in allowed
    )

    result = validate_registry_v3(REPOSITORY_ROOT, check_scope=True)
    assert result["verdict"] == "PASS", result
    assert result["active_goal_id"] == "TC-VNEXT-G07-CANDIDATE"
    assert result["package_count"] == 1
    assert "docs/governance/current_work_packages.json" in result["changed_paths"]
    assert all(
        any(path == root or path.startswith(f"{root}/") for root in allowed)
        for path in result["changed_paths"]
    )


def test_agent_gate_verifier_is_bounded_during_cycle_2_contract_rebinding() -> None:
    registry = json.loads(
        (
            REPOSITORY_ROOT / "docs/governance/current_work_packages.json"
        ).read_text(encoding="utf-8")
    )
    active_slice = {
        **registry["active_slice"],
        "work_kind": "CANDIDATE_HARDENING",
        "slice_id": "G07-CANDIDATE-CONTRACT",
    }

    assert work_packages_v3._agent_gate_path_is_authorized_for_g07(
        "backend/evals/agent_gate_v1/contracts.py",
        registry=registry,
        active_slice=active_slice,
    )
    sealed_slice = {**active_slice, "slice_id": "G07-SEALED-ONE-SHOT"}
    assert work_packages_v3._agent_gate_path_is_authorized_for_g07(
        "backend/evals/agent_gate_v1/candidate_component_verifiers.py",
        registry=registry,
        active_slice=sealed_slice,
    )
    assert not work_packages_v3._agent_gate_path_is_authorized_for_g07(
        "backend/evals/agent_gate_v1/final_gate.py",
        registry=registry,
        active_slice=sealed_slice,
    )


def test_active_registry_uses_explicit_head_for_first_parent_proof(monkeypatch) -> None:
    original_git = work_packages_v3._git
    observed_heads: list[str] = []

    def recording_git(root: Path, *args: str, **kwargs: object) -> str:
        if args[:3] == ("rev-list", "--first-parent", "--reverse"):
            observed_heads.append(args[3])
        return original_git(root, *args, **kwargs)

    monkeypatch.setattr(work_packages_v3, "_git", recording_git)

    result = validate_registry_v3(REPOSITORY_ROOT, head_ref="HEAD^")

    assert result["verdict"] == "PASS", result
    assert observed_heads == ["HEAD^"]
