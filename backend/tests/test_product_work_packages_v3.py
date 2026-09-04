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
        "refs/heads/codex/g07-candidate-cycle-3"
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
        "REPAIR_ACTIVE",
        "PREFLIGHT",
        "EVIDENCE_FROZEN",
        "GATE_RUNNING",
    }
    assert registry["active_slice"]["candidate_cycle"] >= 2
    assert registry["max_parallel_writers"] == 2
    assert [package["package_id"] for package in registry["packages"]] == [
        "WP-G07-INTEGRATOR",
        "WP-G07-TEXT-CONVERGENCE",
        "WP-G07-UI-CONVERGENCE",
    ]
    contributor = registry["packages"][1]
    ui_contributor = registry["packages"][2]
    assert registry["active_slice"]["base_commit"] == (
        "95bcb76a9688a03a0527e02317918ecdbb48bfe2"
    )
    assert contributor["status"] == "MERGED"
    assert contributor["branch"] == "codex/g07-text-convergence-r2-fix"
    assert contributor["remote_branch"] == (
        "origin/codex/g07-text-convergence-r2-fix"
    )
    assert contributor["registry_binding_commit"] == (
        "37eedc23e47dc796c909dde2fcdbabe96b9dcb1d"
    )
    assert contributor["branch_point_commit"] == (
        "6f66067b6c4b33d5cffd5aef5587b3d6b416ec57"
    )
    assert contributor["ready_commit"] == (
        "7ade404ab3d6c47a03b7465bbbfec160af92725d"
    )
    assert contributor["merged_commit"] == (
        "95bcb76a9688a03a0527e02317918ecdbb48bfe2"
    )
    assert contributor["remote_readback_commit"] == contributor["ready_commit"]
    assert contributor["prompt_sha256"] == (
        "089313b369aa389eb05e298d388cba40e3e4621c9690616a0a8f527dd658c4cd"
    )
    assert contributor["prompt_sha256"] == hashlib.sha256(
        (REPOSITORY_ROOT / contributor["prompt_path"]).read_bytes()
    ).hexdigest()
    assert ui_contributor["status"] == "WAITING_FOR_WRITER_SLOT"
    assert ui_contributor["baseline_commit"] == contributor["merged_commit"]
    assert ui_contributor["branch"] == "codex/g07-ui-convergence"
    assert ui_contributor["remote_branch"] == "origin/codex/g07-ui-convergence"
    assert ui_contributor["dialogue_ref"] == (
        "codex-task:01a06c20-ab96-73f1-a285-75186e0f3267"
    )
    assert ui_contributor["worktree_path"] == (
        "C:/Users/18770/.codex/worktrees/30f3/BreezeTravel"
    )
    assert ui_contributor["dependencies"] == ["WP-G07-TEXT-CONVERGENCE"]
    assert ui_contributor["prompt_sha256"] == (
        "7ce9d75e4158533d0d015f7c89cefb7de5440857e60a099f0a3c09d109f04903"
    )
    assert ui_contributor["prompt_sha256"] == hashlib.sha256(
        (REPOSITORY_ROOT / ui_contributor["prompt_path"]).read_bytes()
    ).hexdigest()
    assert not {
        "registry_binding_commit",
        "branch_point_commit",
        "ready_commit",
        "remote_readback_commit",
        "merged_commit",
    } & ui_contributor.keys()
    assert registry["writer_activation"] == "INTEGRATOR_ONLY"

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


def test_g07_later_cycle_active_slice_remains_narrow_and_bound_to_candidate_ref() -> None:
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
    assert active_slice["candidate_cycle"] >= 2
    assert isinstance(active_slice["repair_review_cycle"], int)
    assert active_slice["repair_review_cycle"] >= 0
    assert active_slice["work_kind"] == "CANDIDATE_HARDENING"
    assert set(allowed) == {
        "docs/governance/CURRENT_GOAL.md",
        "docs/governance/current_work_packages.json",
        "docs/governance/work-packages/WP-G07-UI-CONVERGENCE.md",
        "backend/tests/test_product_work_packages_v3.py",
        "frontend/src/app/trip/result/page.tsx",
        "frontend/src/app/trip/result/itinerary-workspace.tsx",
        "frontend/src/app/trip/result/result-navigation.tsx",
        "frontend/src/app/trip/result/result-presentation.ts",
        "frontend/src/app/trip/result/accessible-dialog.tsx",
        "frontend/src/app/login/page.tsx",
        "frontend/e2e/trip-understanding-v3.spec.js",
        "frontend/e2e/g02-product-delivery.spec.js",
        "frontend/e2e/g03-product-delivery.spec.js",
        "frontend/e2e/g03r-result-ui.spec.js",
    }
    assert "docs/governance/product_delivery_gates.json" not in allowed
    assert "docs/governance/current_goal_binding.json" not in allowed
    assert "backend/app/trip_understanding" not in allowed
    assert "frontend/src" not in allowed
    assert not any("frozen_blind" in path for path in allowed)

    packages = registry["packages"]
    assert all(
        sum(
            any(
                path == root or path.startswith(f"{root}/")
                for root in package["owned_paths"]
            )
            for package in packages
        )
        == 1
        for path in allowed
    )

    result = validate_registry_v3(REPOSITORY_ROOT, check_scope=True)
    assert result["verdict"] == "PASS", result
    assert result["active_goal_id"] == "TC-VNEXT-G07-CANDIDATE"
    assert result["package_count"] == 3
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


def test_unmerged_contributor_blocks_final_evidence_but_not_repair_work(
    tmp_path: Path, monkeypatch
) -> None:
    copied_paths = (
        "AGENTS.md",
        "docs/governance/CURRENT_GOAL.md",
        "docs/governance/current_goal_binding.json",
        "docs/governance/product_delivery_gates.json",
        "docs/governance/work-packages/WP-G07-TEXT-CONVERGENCE.md",
        "docs/governance/work-packages/WP-G07-UI-CONVERGENCE.md",
    )
    for relative in copied_paths:
        source = REPOSITORY_ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    registry = json.loads(
        (
            REPOSITORY_ROOT / "docs/governance/current_work_packages.json"
        ).read_text(encoding="utf-8")
    )
    contributor = registry["packages"][1]
    contributor["status"] = "WAITING_FOR_WRITER_SLOT"
    for field in (
        "registry_binding_commit",
        "branch_point_commit",
        "ready_commit",
        "merged_commit",
    ):
        contributor.pop(field, None)
    registry["writer_activation"] = "INTEGRATOR_ONLY"
    registry_path = tmp_path / "docs/governance/current_work_packages.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(work_packages_v3, "_git", lambda *_args, **_kwargs: "")

    repair = validate_registry_v3(tmp_path)
    assert repair["verdict"] == "PASS", repair

    registry["active_slice"]["phase"] = "EVIDENCE_FROZEN"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    final = validate_registry_v3(tmp_path)
    assert final["verdict"] == "FAIL", final
    assert (
        "WP-G07-TEXT-CONVERGENCE:NOT_MERGED_FOR_FINAL_EVIDENCE"
        in final["error_codes"]
    )


def _copy_active_registry_contract(tmp_path: Path) -> tuple[dict[str, object], Path]:
    copied_paths = (
        "AGENTS.md",
        "docs/governance/CURRENT_GOAL.md",
        "docs/governance/current_goal_binding.json",
        "docs/governance/product_delivery_gates.json",
        "docs/governance/work-packages/WP-G07-TEXT-CONVERGENCE.md",
        "docs/governance/work-packages/WP-G07-UI-CONVERGENCE.md",
    )
    for relative in copied_paths:
        source = REPOSITORY_ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    registry = json.loads(
        (
            REPOSITORY_ROOT / "docs/governance/current_work_packages.json"
        ).read_text(encoding="utf-8")
    )
    contributor = registry["packages"][1]
    contributor["status"] = "WAITING_FOR_WRITER_SLOT"
    for field in (
        "registry_binding_commit",
        "branch_point_commit",
        "ready_commit",
        "merged_commit",
    ):
        contributor.pop(field, None)
    registry["writer_activation"] = "INTEGRATOR_ONLY"
    registry_path = tmp_path / "docs/governance/current_work_packages.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    return registry, registry_path


def test_registry_rejects_unknown_phase_and_integrator_contributor_overlap(
    tmp_path: Path, monkeypatch
) -> None:
    registry, registry_path = _copy_active_registry_contract(tmp_path)
    monkeypatch.setattr(work_packages_v3, "_git", lambda *_args, **_kwargs: "")

    registry["active_slice"]["phase"] = "TYPO_PHASE"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    invalid_phase = validate_registry_v3(tmp_path)
    assert invalid_phase["verdict"] == "FAIL", invalid_phase
    assert "ACTIVE_SLICE_PHASE_INVALID" in invalid_phase["error_codes"]

    registry["active_slice"]["phase"] = "REPAIR_ACTIVE"
    registry["packages"][0]["owned_paths"].append(
        "backend/app/trip_understanding/full_text.py"
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    overlap = validate_registry_v3(tmp_path)
    assert overlap["verdict"] == "FAIL", overlap
    assert (
        "WP-G07-INTEGRATOR:WP-G07-TEXT-CONVERGENCE:OWNED_PATHS_OVERLAP"
        in overlap["error_codes"]
    )


def test_ready_package_proves_one_owned_feature_commit(
    tmp_path: Path, monkeypatch
) -> None:
    registry, registry_path = _copy_active_registry_contract(tmp_path)
    contributor = registry["packages"][1]
    ready_commit = "a" * 40
    branch_point = "b" * 40
    contributor.update(
        {
            "status": "READY_TO_MERGE",
            "ready_commit": ready_commit,
            "branch_point_commit": branch_point,
            "registry_binding_commit": "e" * 40,
        }
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    def fake_git(_root: Path, *args: str, **_kwargs: object) -> str:
        if args == ("rev-parse", contributor["remote_branch"]):
            return ready_commit
        if args == ("rev-list", "--parents", "-n", "1", ready_commit):
            return f"{ready_commit} {branch_point}"
        return ""

    monkeypatch.setattr(work_packages_v3, "_git", fake_git)
    monkeypatch.setattr(work_packages_v3, "_git_succeeds", lambda *_args: True)
    monkeypatch.setattr(
        work_packages_v3,
        "_registry_binding_snapshot_errors",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        work_packages_v3,
        "_commit_paths",
        lambda *_args, **_kwargs: (
            "backend/app/trip_understanding/full_text.py",
        ),
    )

    valid = validate_registry_v3(tmp_path)
    assert valid["verdict"] == "PASS", valid

    monkeypatch.setattr(
        work_packages_v3,
        "_remote_branch_tip",
        lambda *_args, **_kwargs: "f" * 40,
    )
    remote_server_mismatch = validate_registry_v3(
        tmp_path,
        require_all_merged=True,
    )
    assert remote_server_mismatch["verdict"] == "FAIL", remote_server_mismatch
    assert (
        "WP-G07-TEXT-CONVERGENCE:REMOTE_READBACK_MISMATCH"
        in remote_server_mismatch["error_codes"]
    )

    def stale_local_git(_root: Path, *args: str, **_kwargs: object) -> str:
        if args == ("rev-parse", contributor["remote_branch"]):
            return "f" * 40
        if args == ("rev-list", "--parents", "-n", "1", ready_commit):
            return f"{ready_commit} {branch_point}"
        return ""

    monkeypatch.setattr(work_packages_v3, "_git", stale_local_git)
    monkeypatch.setattr(
        work_packages_v3,
        "_remote_branch_tip",
        lambda *_args, **_kwargs: ready_commit,
    )
    formal_ignores_stale_tracking_ref = validate_registry_v3(
        tmp_path,
        require_all_merged=True,
    )
    assert (
        "WP-G07-TEXT-CONVERGENCE:REMOTE_READBACK_MISMATCH"
        not in formal_ignores_stale_tracking_ref["error_codes"]
    )
    assert (
        "WP-G07-TEXT-CONVERGENCE:LOCAL_REMOTE_TRACKING_MISMATCH"
        not in formal_ignores_stale_tracking_ref["error_codes"]
    )
    monkeypatch.setattr(work_packages_v3, "_git", fake_git)

    monkeypatch.setattr(
        work_packages_v3,
        "_commit_paths",
        lambda *_args, **_kwargs: ("frontend/src/app/page.tsx",),
    )
    outside_owned = validate_registry_v3(tmp_path)
    assert outside_owned["verdict"] == "FAIL", outside_owned
    assert (
        "WP-G07-TEXT-CONVERGENCE:READY_OUTSIDE_OWNED_PATHS"
        in outside_owned["error_codes"]
    )

    def wrong_parent_git(_root: Path, *args: str, **_kwargs: object) -> str:
        if args == ("rev-parse", contributor["remote_branch"]):
            return ready_commit
        if args == ("rev-list", "--parents", "-n", "1", ready_commit):
            return f"{ready_commit} {'c' * 40}"
        return ""

    monkeypatch.setattr(work_packages_v3, "_git", wrong_parent_git)
    wrong_parent = validate_registry_v3(tmp_path)
    assert wrong_parent["verdict"] == "FAIL", wrong_parent
    assert (
        "WP-G07-TEXT-CONVERGENCE:FEATURE_COMMIT_COUNT_INVALID"
        in wrong_parent["error_codes"]
    )

    def merge_commit_git(_root: Path, *args: str, **_kwargs: object) -> str:
        if args == ("rev-parse", contributor["remote_branch"]):
            return ready_commit
        if args == ("rev-list", "--parents", "-n", "1", ready_commit):
            return f"{ready_commit} {branch_point} {'c' * 40}"
        return ""

    monkeypatch.setattr(work_packages_v3, "_git", merge_commit_git)
    merge_commit = validate_registry_v3(tmp_path)
    assert merge_commit["verdict"] == "FAIL", merge_commit
    assert (
        "WP-G07-TEXT-CONVERGENCE:FEATURE_COMMIT_COUNT_INVALID"
        in merge_commit["error_codes"]
    )

    monkeypatch.setattr(work_packages_v3, "_git", fake_git)
    monkeypatch.setattr(work_packages_v3, "_git_succeeds", lambda *_args: False)
    unrelated_branch_point = validate_registry_v3(tmp_path)
    assert unrelated_branch_point["verdict"] == "FAIL", unrelated_branch_point
    assert (
        "WP-G07-TEXT-CONVERGENCE:FEATURE_ANCESTRY_INVALID"
        in unrelated_branch_point["error_codes"]
    )


def test_formal_candidate_context_requires_all_contributors_merged(
    tmp_path: Path, monkeypatch
) -> None:
    _registry, _registry_path = _copy_active_registry_contract(tmp_path)
    monkeypatch.setattr(work_packages_v3, "_git", lambda *_args, **_kwargs: "")

    report = validate_registry_v3(tmp_path, require_all_merged=True)

    assert report["verdict"] == "FAIL", report
    assert (
        "WP-G07-TEXT-CONVERGENCE:NOT_MERGED_FOR_FINAL_EVIDENCE"
        in report["error_codes"]
    )


def test_unknown_role_is_rejected_and_next_goal_preparation_is_not_current_work(
    tmp_path: Path, monkeypatch
) -> None:
    registry, registry_path = _copy_active_registry_contract(tmp_path)
    next_package = dict(registry["packages"][1])
    next_package.update(
        {
            "package_id": "WP-G08-PREP",
            "goal_id": "TC-VNEXT-G08-FUTURE",
            "status": "PREPARED_NOT_INTEGRATED",
            "branch": "codex/g08-prep",
            "remote_branch": "origin/codex/g08-prep",
            "dialogue_ref": "codex-task:g08-prep",
            "worktree_path": "D:/CODEX/BreezeTravel-g08-prep",
            "owned_paths": ["backend/eval_data/g08_prep"],
        }
    )
    registry["packages"].append(next_package)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(work_packages_v3, "_git", lambda *_args, **_kwargs: "")

    report = validate_registry_v3(tmp_path, require_all_merged=True)
    assert (
        "WP-G08-PREP:NOT_MERGED_FOR_FINAL_EVIDENCE"
        not in report["error_codes"]
    )
    assert "MAX_PREPARED_NEXT_GOAL_PACKAGES_EXCEEDED" in report["error_codes"]
    assert "NEXT_GOAL_PACKAGE_ID_MISMATCH" in report["error_codes"]

    registry["max_prepared_next_goal_packages"] = 1
    next_package["goal_id"] = registry["next_goal_id"]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    valid_next_preparation = validate_registry_v3(tmp_path)
    assert valid_next_preparation["verdict"] == "PASS", valid_next_preparation

    next_package["goal_id"] = "TC-VNEXT-G09-SKIPPED"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    skipped_goal = validate_registry_v3(tmp_path)
    assert skipped_goal["verdict"] == "FAIL", skipped_goal
    assert "NEXT_GOAL_PACKAGE_ID_MISMATCH" in skipped_goal["error_codes"]

    registry["next_goal_id"] = "TC-VNEXT-G09-SKIPPED"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    self_consistent_but_unbound_next = validate_registry_v3(tmp_path)
    assert self_consistent_but_unbound_next["verdict"] == "FAIL"
    assert (
        "NEXT_GOAL_BINDING_MISMATCH"
        in self_consistent_but_unbound_next["error_codes"]
    )

    next_package["role"] = "OBSERVER"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    invalid_role = validate_registry_v3(tmp_path)
    assert invalid_role["verdict"] == "FAIL", invalid_role
    assert "PACKAGE_ROLE_INVALID" in invalid_role["error_codes"]

    registry["packages"][0]["goal_id"] = registry["next_goal_id"]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    wrong_integrator_goal = validate_registry_v3(tmp_path)
    assert wrong_integrator_goal["verdict"] == "FAIL", wrong_integrator_goal
    assert "ACTIVE_INTEGRATOR_GOAL_MISMATCH" in wrong_integrator_goal["error_codes"]


def test_package_checkout_binds_worktree_branch_prompt_and_activation(
    tmp_path: Path, monkeypatch
) -> None:
    registry, registry_path = _copy_active_registry_contract(tmp_path)
    contributor = registry["packages"][1]
    contributor.update(
        {
            "status": "IN_PROGRESS",
            "worktree_path": tmp_path.resolve().as_posix(),
            "registry_binding_commit": "c" * 40,
        }
    )
    registry["writer_activation"] = "INTEGRATOR_AND_CONTRIBUTOR"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    def fake_git(_root: Path, *args: str, **_kwargs: object) -> str:
        if args == ("symbolic-ref", "--short", "-q", "HEAD"):
            return str(contributor["branch"])
        return ""

    monkeypatch.setattr(work_packages_v3, "_git", fake_git)
    monkeypatch.setattr(work_packages_v3, "_git_succeeds", lambda *_args: True)
    monkeypatch.setattr(
        work_packages_v3,
        "_registry_binding_snapshot_errors",
        lambda *_args, **_kwargs: (),
    )

    valid = validate_registry_v3(
        tmp_path,
        package_id="WP-G07-TEXT-CONVERGENCE",
    )
    assert valid["verdict"] == "PASS", valid

    contributor["worktree_path"] = (tmp_path / "elsewhere").as_posix()
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    mismatch = validate_registry_v3(
        tmp_path,
        package_id="WP-G07-TEXT-CONVERGENCE",
    )
    assert mismatch["verdict"] == "FAIL", mismatch
    assert (
        "WP-G07-TEXT-CONVERGENCE:CHECKOUT_WORKTREE_MISMATCH"
        in mismatch["error_codes"]
    )


def test_registry_binding_commit_pins_prompt_and_owned_path_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    registry, _registry_path = _copy_active_registry_contract(tmp_path)
    package = dict(registry["packages"][1])
    binding_commit = "c" * 40
    package.update(
        {
            "status": "IN_PROGRESS",
            "registry_binding_commit": binding_commit,
        }
    )
    snapshot_registry = json.loads(json.dumps(registry))
    snapshot_registry["packages"][1]["status"] = "WAITING_FOR_WRITER_SLOT"
    prompt_bytes = (
        tmp_path / package["prompt_path"]
    ).read_bytes()

    def fake_blob(_root: Path, _commit: str, path: str) -> bytes:
        if path == "docs/governance/current_work_packages.json":
            return json.dumps(snapshot_registry).encode("utf-8")
        if path == package["prompt_path"]:
            return prompt_bytes
        raise AssertionError(path)

    monkeypatch.setattr(work_packages_v3, "_git_blob_bytes", fake_blob)

    assert work_packages_v3._registry_binding_snapshot_errors(
        tmp_path, package
    ) == ()

    package["owned_paths"] = ["frontend/src"]
    errors = work_packages_v3._registry_binding_snapshot_errors(tmp_path, package)
    assert (
        "WP-G07-TEXT-CONVERGENCE:REGISTRY_BINDING_SNAPSHOT_MISMATCH"
        in errors
    )


def test_scope_check_uses_contributor_owned_paths_on_contributor_branch(
    tmp_path: Path, monkeypatch
) -> None:
    registry, registry_path = _copy_active_registry_contract(tmp_path)
    contributor = registry["packages"][1]
    contributor["status"] = "IN_PROGRESS"
    contributor["registry_binding_commit"] = "c" * 40
    registry["writer_activation"] = "INTEGRATOR_AND_CONTRIBUTOR"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    branch_point = "d" * 40

    def fake_git(_root: Path, *args: str, **_kwargs: object) -> str:
        if args == ("symbolic-ref", "--short", "-q", "HEAD"):
            return str(contributor["branch"])
        if args[:2] == ("merge-base", "HEAD"):
            return branch_point
        return ""

    monkeypatch.setattr(work_packages_v3, "_git", fake_git)
    monkeypatch.setattr(
        work_packages_v3,
        "_registry_binding_snapshot_errors",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        work_packages_v3,
        "_working_paths",
        lambda *_args, **_kwargs: (
            "backend/app/trip_understanding/full_text.py",
        ),
    )

    valid = validate_registry_v3(tmp_path, check_scope=True)
    assert valid["verdict"] == "PASS", valid
    assert valid["scope_base_commit"] == branch_point

    monkeypatch.setattr(
        work_packages_v3,
        "_working_paths",
        lambda *_args, **_kwargs: ("docs/governance/CURRENT_GOAL.md",),
    )
    outside_owned = validate_registry_v3(tmp_path, check_scope=True)
    assert outside_owned["verdict"] == "FAIL", outside_owned
    assert "ACTIVE_SLICE_SCOPE_VIOLATION" in outside_owned["error_codes"]
