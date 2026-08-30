from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from evals.agent_gate_v1.contracts import (
    AutomatedProductGateContract,
)


ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE = ROOT / "docs" / "governance"
PLANNED = GOVERNANCE / "goals" / "planned"
COMPLETED = GOVERNANCE / "goals" / "completed"
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


def _goal_contract(sequence: int) -> Path:
    """Return the current/future contract without assuming G01 stays active."""
    planned = list(PLANNED.glob(f"TC-VNEXT-G{sequence:02d}-*.md"))
    if planned:
        assert len(planned) == 1, (sequence, planned)
        return planned[0]
    current = _text(CURRENT)
    goal_marker = f"TC-VNEXT-G{sequence:02d}-"
    current_goal = re.search(r"^Goal ID: (\S+)$", current, re.MULTILINE)
    if current_goal and current_goal.group(1).startswith(goal_marker):
        return CURRENT
    candidates = list(COMPLETED.glob(f"TC-VNEXT-G{sequence:02d}-*.md"))
    assert len(candidates) == 1, (sequence, candidates)
    return candidates[0]


def test_authority_files_separate_delivery_and_candidate_evidence() -> None:
    for path in (
        ROOT / "AGENTS.md",
        GOVERNANCE / "PROGRAM.md",
        GOVERNANCE / "RELEASE_GATES.md",
    ):
        content = _text(path)
        assert "PRODUCT_DELIVERY_GATE" in content, path
        assert "HARDENED_CANDIDATE_GATE" in content, path
        assert "FROZEN_G07_ASSET" in content, path

    protocol = _text(GOVERNANCE / "AGENT_GATE_PROTOCOL.md")
    for level in EVIDENCE_LEVELS:
        assert level in protocol
    assert "FROZEN_G07_ASSET" in protocol
    assert "适用范围：仅`TC-VNEXT-G07-CANDIDATE`" in protocol


def test_every_pre_h1_goal_uses_the_phase_appropriate_gate() -> None:
    goals = [_goal_contract(sequence) for sequence in range(1, 8)]
    for path in goals[:6]:
        content = _text(path)
        assert "PRODUCT_DELIVERY_PASS" in content, path
        assert "Gate profile：`PRODUCT_DELIVERY_GATE`" in content, path
        assert "AGENT_GATE_PASS" not in content, path
        assert "ADR-013" in content, path
        assert "H1" in content and "NOT_RUN" in content, path

    g07 = _text(_goal_contract(7))
    assert "HARDENED_CANDIDATE_GATE_PASS" in g07
    assert "Gate profile：`HARDENED_CANDIDATE_GATE`" in g07
    assert "Agent Gate Protocol" in g07
    assert "H1" in g07 and "NOT_RUN" in g07
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


def test_current_goal_is_the_only_active_goal() -> None:
    binding = json.loads(_text(GOVERNANCE / "current_goal_binding.json"))
    current = _text(CURRENT)
    assert re.search(
        rf"^Goal ID: {re.escape(binding['goal_id'])}$", current, re.MULTILINE
    )
    assert re.search(
        rf"^Status: {re.escape(binding['status'])}$", current, re.MULTILINE
    )
    for path in PLANNED.glob("TC-VNEXT-G*.md"):
        assert "- Status：`DRAFT`" in _text(path)
    assert "PRODUCT_DELIVERY_PASS" in current
    assert "H1、公网、生产、商业：`NOT_RUN`" in current
    if binding.get("program_state") == "CORE_MVP_OWNER_REVIEW_PENDING":
        assert binding["goal_id"] == "CORE_MVP_OWNER_REVIEW_PENDING"
        assert binding["status"] == "OWNER_REVIEW_PENDING"
        assert "Status: APPROVED" not in current
        assert "Status: IN_PROGRESS" not in current
        assert "G04：`NOT_ACTIVATED`" in current


def test_g01_to_g06_use_delivery_and_only_g07_uses_hardened_gate() -> None:
    current_binding = json.loads(
        _text(GOVERNANCE / "current_goal_binding.json")
    )
    sequence = current_binding["goal_sequence"]
    expected_profile = (
        "HARDENED_CANDIDATE_GATE" if sequence == 7 else "PRODUCT_DELIVERY_GATE"
    )
    expected_phase = (
        "CORE_MVP"
        if sequence <= 3
        else "PRODUCT_ENHANCEMENT"
        if sequence <= 6
        else "CANDIDATE_HARDENING"
    )
    assert 1 <= sequence <= 7
    assert current_binding["gate_profile"] == expected_profile
    assert current_binding["schema_version"] == "current-goal-binding-v3"
    assert current_binding["mainline_phase"] == expected_phase
    assert current_binding["work_package_registry_path"] == (
        "docs/governance/current_work_packages.json"
    )

    work_packages = json.loads(
        _text(GOVERNANCE / "current_work_packages.json")
    )
    assert work_packages["schema_version"] == "work-package-registry-v3"
    assert work_packages["scope_guard_version"] == "core-mainline-v1"
    assert re.fullmatch(r"[0-9a-f]{64}", work_packages["scope_policy_sha256"])
    assert work_packages["active_goal_sequence"] == sequence
    assert work_packages["gate_profile"] == expected_profile
    owner_review_hold = (
        current_binding.get("program_state") == "CORE_MVP_OWNER_REVIEW_PENDING"
    )
    if owner_review_hold:
        assert sequence == 3
        assert current_binding["goal_id"] == "CORE_MVP_OWNER_REVIEW_PENDING"
        assert current_binding["next_goal_id"] == "TC-VNEXT-G04-SCREENSHOT"
        assert current_binding["next_goal_status"] == "NOT_ACTIVATED"
        assert work_packages["active_slice"]["work_kind"] == "GOAL_TRANSITION"
        assert work_packages["active_slice"]["product_progress"] == "NONE"
        assert work_packages["writer_activation"] == "NONE"
    elif sequence <= 3:
        assert work_packages["active_slice"]["work_kind"] in {
            "PRODUCT",
            "BLOCKING_DEFECT",
            "GOAL_TRANSITION",
        }
        assert work_packages["active_slice"]["product_progress"] != "NONE"
        assert work_packages["active_slice"]["repair_review_cycle"] <= 2

    policy = json.loads(
        _text(ROOT / "backend/eval_data/agent_gate_v1/authority_policy.json")
    )
    frozen_candidate_policy = {
        item["goal_sequence"]: item["gate_profile"]
        for item in policy["goal_bindings"]
    }
    assert frozen_candidate_policy == {
        1: "CORE_AGENT_GATE",
        2: "CORE_AGENT_GATE",
        3: "CORE_AGENT_GATE",
        4: "CORE_AGENT_GATE",
        5: "CORE_AGENT_GATE",
        6: "CORE_AGENT_GATE",
        7: "HARDENED_CANDIDATE_GATE",
    }


def test_goal_phases_parallel_contracts_and_core_transitions_are_consistent() -> None:
    active_sequence = json.loads(
        _text(GOVERNANCE / "current_goal_binding.json")
    )["goal_sequence"]
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
        path = _goal_contract(sequence)
        content = _text(path)
        expected_profile = (
            "HARDENED_CANDIDATE_GATE"
            if sequence == 7
            else "PRODUCT_DELIVERY_GATE"
        )
        assert f"Mainline phase：`{phase}`" in content
        assert f"Gate profile：`{expected_profile}`" in content
        if sequence >= active_sequence:
            assert "## Parallel work packages" in content
            assert "Product progress" in content and "Governance ratio" in content

    core_goals = "\n".join(
        _text(_goal_contract(sequence))
        for sequence in range(1, 7)
    )
    assert "AGENT_GATE_PASS" not in core_goals
    g03 = _text(_goal_contract(3))
    assert "CORE_MVP_OWNER_REVIEW_PENDING" in g03
    assert "不得自动激活G04" in g03


def test_checked_in_work_package_registry_binds_guidance_and_active_goal() -> None:
    path = GOVERNANCE / "current_work_packages.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    assert registry["schema_version"] == "work-package-registry-v3"
    binding = json.loads(_text(GOVERNANCE / "current_goal_binding.json"))
    assert registry["active_goal_id"] == binding["goal_id"]
    assert registry["active_goal_sequence"] == binding["goal_sequence"]
    assert registry["mainline_phase"] == binding["mainline_phase"]
    assert registry["gate_profile"] == binding["gate_profile"]
    assert registry["guidance_sha256"] == _sha256(ROOT / "AGENTS.md")
    integrator = registry["packages"][0]
    assert integrator["execution_mode"] == "PRIMARY_INTEGRATOR_DIALOGUE"
    assert integrator["remote_branch"] == f"origin/{integrator['branch']}"
    assert integrator["worktree_path"] is not None
    assert registry["e2e_after_all_merges"] is True
    active_integrators = [
        package
        for package in registry["packages"]
        if package["role"] == "INTEGRATOR"
        and package["status"] in {"IN_PROGRESS", "READY_TO_MERGE"}
    ]
    if registry.get("program_state") == "CORE_MVP_OWNER_REVIEW_PENDING":
        assert active_integrators == []
        assert len(registry["packages"]) == 1
        assert integrator["goal_id"] == "TC-VNEXT-G03-TOP3-AUDIT"
        assert integrator["status"] == "MERGED"
        assert re.fullmatch(r"[0-9a-f]{40}", integrator["ready_commit"])
        assert re.fullmatch(r"[0-9a-f]{40}", integrator["merged_commit"])
        assert registry["next_goal_id"] == "TC-VNEXT-G04-SCREENSHOT"
        assert registry["next_goal_status"] == "NOT_ACTIVATED"
        assert registry["writer_activation"] == "NONE"
    else:
        assert len(active_integrators) == 1


def test_g03_owner_review_hold_keeps_external_evidence_and_release_not_run() -> None:
    binding = json.loads(_text(GOVERNANCE / "current_goal_binding.json"))
    if binding.get("program_state") != "CORE_MVP_OWNER_REVIEW_PENDING":
        return

    current = _text(CURRENT)
    match = re.search(
        r"<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE\n(?P<payload>\{.*?\})\n-->",
        current,
        re.DOTALL,
    )
    assert match is not None
    state = json.loads(match.group("payload"))
    assert state["last_completed_goal_id"] == "TC-VNEXT-G03-TOP3-AUDIT"
    assert state["next_goal_id"] == "TC-VNEXT-G04-SCREENSHOT"
    assert state["next_activated"] is False
    assert state["g04_status"] == "NOT_ACTIVATED"
    for field in (
        "fux03_status",
        "h1_status",
        "public_network_status",
        "production_status",
        "commercial_status",
    ):
        assert state[field] == "NOT_RUN"
    for field in ("release_status", "deployment_status", "main_merge_status"):
        assert state[field] == "NOT_REQUESTED"


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

    g02 = _text(_goal_contract(2))
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
    g04 = _text(_goal_contract(4))
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
