from __future__ import annotations

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


def test_g04_and_g05_delivery_archives_remain_verifiable_after_transition() -> None:
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

    assert g04_result["verdict"] == "PASS", g04_result
    assert g05_result["verdict"] == "PASS", g05_result
    assert g04_archive.startswith("# COMPLETED GOAL：V0.4 截图与文本一致")
    assert g05_archive.startswith("# COMPLETED GOAL：V0.5 三城有来源知识层")
    for archive in (g04_archive, g05_archive):
        assert '"goal_archived": true' in archive
        assert '"next_activated": true' in archive
    assert "33357640834" in g04_archive
    assert "33389553342" in g05_archive
    assert "33389970986" in g05_archive


def test_g05_archive_and_g06_approved_activation_are_unambiguous() -> None:
    governance = REPOSITORY_ROOT / "docs/governance"
    current_goal = (governance / "CURRENT_GOAL.md").read_text(encoding="utf-8")
    archive_path = governance / (
        "goals/completed/TC-VNEXT-G05-CITY-KNOWLEDGE.md"
    )
    archive = archive_path.read_text(encoding="utf-8")
    binding = json.loads(
        (governance / "current_goal_binding.json").read_text(encoding="utf-8")
    )
    registry = json.loads(
        (governance / "current_work_packages.json").read_text(encoding="utf-8")
    )
    current_state = _machine_state(current_goal)
    archived_state = _machine_state(archive)

    assert list((governance / "goals/planned").glob("TC-VNEXT-G06-*.md")) == []
    assert list((governance / "goals/completed").glob("TC-VNEXT-G05-*.md")) == [
        archive_path
    ]
    assert current_state == {
        "schema_version": "product-delivery-current-goal-state-v1",
        "program_id": "TC-VNEXT-2026",
        "goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
        "goal_status": "APPROVED",
        "gate_profile": "PRODUCT_DELIVERY_GATE",
        "required_gate": "Consent & Share Gate + PRODUCT_DELIVERY_PASS",
        "completion_status": "PENDING",
        "gate_result": "PRODUCT_DELIVERY_NOT_RUN",
        "goal_archived": False,
        "last_completed_goal_id": "TC-VNEXT-G05-CITY-KNOWLEDGE",
        "next_goal_id": "TC-VNEXT-G07-CANDIDATE",
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
    assert binding["goal_sequence"] == registry["active_goal_sequence"] == 6
    assert binding["status"] == current_state["goal_status"] == "APPROVED"
    assert binding["program_state"] == registry["program_state"]
    assert binding["predecessor_goal_id"] == archived_state["goal_id"]
    assert registry["active_slice"]["work_kind"] == "GOAL_TRANSITION"
    assert registry["active_slice"]["phase"] == "GOAL_TRANSITION"
    assert registry["active_slice"]["product_progress"] == "NONE"
    assert registry["max_parallel_writers"] == 2
    assert [package["package_id"] for package in registry["packages"]] == [
        "WP-G06-INTEGRATOR"
    ]

    completion = archive.split("## Completion record", maxsplit=1)[1].split(
        "## Stop conditions", maxsplit=1
    )[0]
    assert "PENDING" not in completion
    assert "待提交" not in completion
    assert "待合并" not in completion

    for token in (
        "consent合同通过后`033_user_memory_and_feedback.sql`",
        "记忆默认关闭",
        "产品记忆不等于训练同意",
        "分享链接使用`/share/{share_ref}#s=<secret>`",
        "当前registry只激活唯一集成者的治理占位",
        "H1/商业/公网/生产/release/deploy/`main`需批准",
    ):
        assert token in current_goal


def test_g06_transition_excludes_product_and_accepts_current_diff() -> None:
    registry = json.loads(
        (
            REPOSITORY_ROOT / "docs/governance/current_work_packages.json"
        ).read_text(encoding="utf-8")
    )
    allowed = registry["active_slice"]["allowed_paths"]
    assert not any(path.startswith("backend/evals/agent_gate_v1/") for path in allowed)

    result = validate_registry_v3(REPOSITORY_ROOT, check_scope=True)
    assert result["verdict"] == "PASS", result
    assert result["active_goal_id"] == "TC-VNEXT-G06-MEMORY-SHARE"
    assert result["package_count"] == 1
    assert (
        "docs/governance/goals/completed/TC-VNEXT-G05-CITY-KNOWLEDGE.md"
        in result["changed_paths"]
    )
    assert not any(
        path.startswith(("backend/app/", "frontend/src/"))
        for path in result["changed_paths"]
    )
    assert not any(
        path.startswith("backend/evals/agent_gate_v1/")
        for path in result["changed_paths"]
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
