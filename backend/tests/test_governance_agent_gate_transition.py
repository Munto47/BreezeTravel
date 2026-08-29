from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from evals.agent_gate_v1.contracts import (
    AutomatedProductGateContract,
    WorkPackageRegistry,
)


ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE = ROOT / "docs" / "governance"
PLANNED = GOVERNANCE / "goals" / "planned"
CURRENT = GOVERNANCE / "CURRENT_GOAL.md"
G01_AUTOMATED_GATE = (
    ROOT / "backend" / "eval_data" / "agent_gate_v1" / "g01_automated_product_gate.json"
)

EVIDENCE_LEVELS = (
    "AUTOMATED_TEST",
    "LIVE_PROVIDER_EVIDENCE",
    "MULTI_AGENT_SIMULATED_REVIEW",
    "SEALED_AGENT_BLIND",
    "HUMAN_USABILITY",
    "PRODUCTION_EVIDENCE",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_authority_files_bind_the_same_agent_gate_evidence_levels() -> None:
    for path in (
        ROOT / "AGENTS.md",
        GOVERNANCE / "AGENT_GATE_PROTOCOL.md",
        GOVERNANCE / "PROGRAM.md",
        GOVERNANCE / "RELEASE_GATES.md",
    ):
        content = _text(path)
        for level in EVIDENCE_LEVELS:
            assert level in content, f"{path} does not bind {level}"
        assert "AGENT_GATE_PASS" in content


def test_every_pre_h1_goal_uses_agent_gate_and_keeps_human_boundary() -> None:
    goals = sorted(PLANNED.glob("TC-VNEXT-G*.md"))
    assert [path.stem.split("-")[2] for path in goals] == [
        "G01",
        "G02",
        "G03",
        "G04",
        "G05",
        "G06",
        "G07",
    ]
    for path in goals:
        content = _text(path)
        assert "AGENT_GATE_PASS" in content, path
        assert "Agent Gate Protocol" in content, path
        assert "ADR-013" in content, path
        assert "H1" in content and "NOT_RUN" in content, path

    g07 = _text(PLANNED / "TC-VNEXT-G07-CANDIDATE.md")
    assert "VNEXT_CANDIDATE_READY_AGENT_VERIFIED" in g07
    assert "Next Goal activated：固定`NO_PENDING_HUMAN_APPROVAL`" in g07


def test_active_contract_no_longer_uses_deprecated_pre_h1_hitl_requirements() -> None:
    current_contract = _text(CURRENT).split("## Checkpoint ledger", maxsplit=1)[0]
    active_contracts = "\n".join(
        [
            current_contract,
            _text(GOVERNANCE / "PROGRAM.md"),
            _text(GOVERNANCE / "RELEASE_GATES.md"),
            *(_text(path) for path in sorted(PLANNED.glob("TC-VNEXT-G*.md"))),
        ]
    )
    deprecated = (
        "HITL_PENDING",
        "BLOCKED_PENDING_WRITTEN_PERMISSION",
        "双人真人标注",
        "两名独立标注员",
        "人工校正转写",
        "外部custodian",
        "高德书面持久化许可",
    )
    for phrase in deprecated:
        assert phrase not in active_contracts

    assert "OWNER_ATTESTED_EXISTING_AUTHORIZATION" in active_contracts
    assert "NOT_EXPOSED_BY_PROVIDER" in active_contracts


def test_current_goal_is_the_only_active_goal_and_g02_is_not_activated() -> None:
    current = _text(CURRENT)
    assert re.search(r"^Goal ID: TC-VNEXT-G01-TEXT-CARDS$", current, re.MULTILINE)
    assert re.search(r"^Status: IN_PROGRESS$", current, re.MULTILINE)
    for path in PLANNED.glob("TC-VNEXT-G*.md"):
        assert "- Status：`DRAFT`" in _text(path)
    assert "AGENT_GATE_NOT_RUN" in current
    assert "H1、公网、生产、商业：`NOT_RUN`" in current


def test_g01_to_g06_use_core_and_only_g07_uses_hardened_gate() -> None:
    current_binding = json.loads(
        _text(GOVERNANCE / "current_goal_binding.json")
    )
    assert current_binding["goal_sequence"] == 1
    assert current_binding["gate_profile"] == "CORE_AGENT_GATE"
    assert current_binding["schema_version"] == "current-goal-binding-v2"
    assert current_binding["mainline_phase"] == "CORE_MVP"
    assert current_binding["work_package_registry_path"] == (
        "docs/governance/current_work_packages.json"
    )

    work_packages = json.loads(
        _text(GOVERNANCE / "current_work_packages.json")
    )
    assert work_packages["scope_guard_version"] == "scope-guard-v1"
    assert re.fullmatch(r"[0-9a-f]{64}", work_packages["scope_policy_sha256"])
    assert work_packages["active_slice"]["work_kind"] in {
        "PRODUCT",
        "CURRENT_GATE_FIX",
        "EVAL_INFRA",
    }
    assert work_packages["active_slice"]["work_kind"] != "HARDENING"
    assert work_packages["active_slice"]["phase"] == "IMPLEMENTING"

    policy = json.loads(
        _text(ROOT / "backend/eval_data/agent_gate_v1/authority_policy.json")
    )
    observed = {
        item["goal_sequence"]: item["gate_profile"]
        for item in policy["goal_bindings"]
    }
    assert observed == {
        1: "CORE_AGENT_GATE",
        2: "CORE_AGENT_GATE",
        3: "CORE_AGENT_GATE",
        4: "CORE_AGENT_GATE",
        5: "CORE_AGENT_GATE",
        6: "CORE_AGENT_GATE",
        7: "HARDENED_CANDIDATE_GATE",
    }


def test_goal_phases_parallel_contracts_and_core_transitions_are_consistent() -> None:
    expected_phases = {
        1: "CORE_MVP",
        2: "CORE_MVP",
        3: "CORE_MVP",
        4: "PRODUCT_ENHANCEMENT",
        5: "PRODUCT_ENHANCEMENT",
        6: "PRODUCT_ENHANCEMENT",
        7: "CANDIDATE_HARDENING",
    }
    for sequence, phase in expected_phases.items():
        path = next(PLANNED.glob(f"TC-VNEXT-G{sequence:02d}-*.md"))
        content = _text(path)
        expected_profile = (
            "HARDENED_CANDIDATE_GATE"
            if sequence == 7
            else "CORE_AGENT_GATE"
        )
        assert f"Mainline phase：`{phase}`" in content
        assert f"Gate profile：`{expected_profile}`" in content
        assert "## Parallel work packages" in content
        assert "Product progress" in content and "Governance ratio" in content

    core_goals = "\n".join(
        _text(next(PLANNED.glob(f"TC-VNEXT-G{sequence:02d}-*.md")))
        for sequence in range(1, 7)
    )
    assert "登记到仓库外Goal pass ledger" not in core_goals
    assert "创建generation" not in core_goals
    g03 = _text(PLANNED / "TC-VNEXT-G03-TOP3-AUDIT.md")
    assert "自动激活G04" in g03 and "不新增HITL" in g03


def test_checked_in_work_package_registry_binds_guidance_and_active_goal() -> None:
    path = GOVERNANCE / "current_work_packages.json"
    registry = WorkPackageRegistry.model_validate_json(path.read_bytes())
    assert registry.schema_version == "work-package-registry-v2"
    assert registry.active_goal_id == "TC-VNEXT-G01-TEXT-CARDS"
    assert registry.mainline_phase == "CORE_MVP"
    assert registry.guidance_sha256 == _sha256(ROOT / "AGENTS.md")
    assert len(
        [
            package
            for package in registry.packages
            if package.role == "INTEGRATOR"
            and package.status in {"IN_PROGRESS", "READY_TO_MERGE"}
        ]
    ) == 1
    integrator = registry.packages[0]
    assert integrator.execution_mode == "PRIMARY_INTEGRATOR_DIALOGUE"
    assert integrator.remote_branch == f"origin/{integrator.branch}"
    assert integrator.worktree_path is not None
    assert registry.e2e_after_all_merges is True


def test_function_dialogue_prompt_and_g02_writer_schedule_are_locked() -> None:
    template = _text(GOVERNANCE / "WORK_PACKAGE_PROMPT_TEMPLATE.md")
    for marker in (
        "prompt_schema_version: work-package-prompt-v1",
        "registry_activation_commit:",
        "remote_branch:",
        "worktree_path:",
        "must_not_merge: true",
        "must_not_modify_goal_or_registry: true",
        "subagent_read_only: true",
        "READY_TO_MERGE | IN_PROGRESS | BLOCKED_EXTERNAL",
    ):
        assert marker in template

    for path in (
        ROOT / "AGENTS.md",
        GOVERNANCE / "PROGRAM.md",
        GOVERNANCE / "PRODUCT_MAINLINE_EXECUTION_GUIDE.md",
        GOVERNANCE / "GOAL_CONTRACT_TEMPLATE.md",
        GOVERNANCE / "RELEASE_GATES.md",
        GOVERNANCE / "AGENT_GATE_PROTOCOL.md",
    ):
        content = _text(path)
        assert "WAITING_FOR_WRITER_SLOT" in content, path
        assert "独立" in content and "worktree" in content, path

    g02 = _text(PLANNED / "TC-VNEXT-G02-MAP-STAY.md")
    expected = (
        "WP-G02-STAY-DOMAIN → WP-G02-MAP-STAY-BACKEND → "
        "WP-G02-MAP-THEATER-UI → E2E"
    )
    assert expected in g02
    assert "codex/wp-g02-map-theater-ui" in g02
    assert "codex/wp-g02-stay-domain" in g02
    assert "codex/wp-g02-map-stay-backend" in g02


def test_checked_in_gate_contracts_default_to_clean_checkout() -> None:
    root = ROOT / "backend/eval_data/agent_gate_v1"
    for sequence in range(1, 8):
        contract = AutomatedProductGateContract.model_validate_json(
            (root / f"g{sequence:02d}_automated_product_gate.json").read_bytes()
        )
        assert contract.isolation.mode == "FRESH_CLEAN_CHECKOUT"


def test_future_product_contracts_keep_the_approved_boundaries() -> None:
    api = _text(ROOT / "docs/product/TRIP_CHECK_API_CONTRACT.md")
    g04 = _text(PLANNED / "TC-VNEXT-G04-SCREENSHOT.md")
    g05 = _text(PLANNED / "TC-VNEXT-G05-CITY-KNOWLEDGE.md")
    g06 = _text(PLANNED / "TC-VNEXT-G06-MEMORY-SHARE.md")
    g07 = _text(PLANNED / "TC-VNEXT-G07-CANDIDATE.md")
    assert "SCREENSHOT_BATCH" in api and "禁止JSON Base64" in api
    assert "SCREENSHOT_BATCH" in g04 and "禁止Base64 JSON" in g04
    assert "不授权抓取小红书" in g05
    assert "记忆默认关闭" in g06 and "训练/eval consent" in g06
    assert "NOT_REQUIRED_WITH_RATIONALE" in g07
    assert "不得因为旧代码存在默认恢复" in g07


def test_checkpoint_ledger_never_has_two_consecutive_none_progress_rows() -> None:
    rows = [line for line in _text(CURRENT).splitlines() if line.startswith("| 2026-")]
    progress = []
    for row in rows:
        match = re.search(
            r"Product progress\s*=\s*(UI|API|MODEL|PROVIDER|EVAL_METRIC|NONE)",
            row,
        )
        if match:
            progress.append((match.group(1), row))
    assert progress, "checkpoint ledger has no machine-readable Product progress"
    exceptions = 0
    for previous, current in zip(progress, progress[1:]):
        if previous[0] == current[0] == "NONE":
            assert "GOVERNANCE_SCOPE_GUARD" in current[1]
            exceptions += 1
    assert exceptions <= 1


def test_g01_current_suite_deselects_only_the_stale_p6_home_copy_assertion() -> None:
    contract = json.loads(_text(G01_AUTOMATED_GATE))
    current_suite = next(
        check for check in contract["checks"] if check["check_id"] == "backend.current_suite"
    )
    argv = current_suite["argv"]
    stale_node_id = (
        "tests/test_trip_check_p6_public_capability_claims.py::"
        "test_primary_home_entry_does_not_expose_frozen_planner_routes"
    )

    assert f"--deselect={stale_node_id}" in argv
    assert "--ignore=tests/test_trip_check_p6_public_capability_claims.py" not in argv
    assert sum(argument.startswith("--deselect=") for argument in argv) == 1


def test_legacy_human_v1_schemas_are_byte_frozen() -> None:
    legacy = ROOT / "backend" / "eval_data" / "trip_text_cards_v1"
    assert _sha256(legacy / "annotation.schema.json") == (
        "4f82afbea940fe6a71b5907ad034d116932dd2547f503a7271594b7620c6fee7"
    )
    assert _sha256(legacy / "adjudication.schema.json") == (
        "7d97924134f35a65b5691c4bf525c54a94c1576ce0bc8733f43add3c819507fb"
    )


def test_agent_v2_schemas_cannot_claim_human_evidence() -> None:
    agent_root = ROOT / "backend" / "eval_data" / "trip_text_cards_agent_v2"
    combined = "\n".join(
        _text(path) for path in sorted(agent_root.glob("*.schema.json"))
    )
    assert "human_label" not in combined
    assert "is_authorized_human" not in combined
    assert "agent_reference" in combined
    assert '"title": "AgentAdjudicationBundle"' in combined
