from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE = ROOT / "docs" / "governance"
PLANNED = GOVERNANCE / "goals" / "planned"
CURRENT = GOVERNANCE / "CURRENT_GOAL.md"

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


def test_checkpoint_ledger_never_has_two_consecutive_none_progress_rows() -> None:
    progress = re.findall(
        r"Product progress\s*=\s*(UI|API|MODEL|PROVIDER|EVAL_METRIC|NONE)",
        _text(CURRENT),
    )
    assert progress, "checkpoint ledger has no machine-readable Product progress"
    assert all(pair != ("NONE", "NONE") for pair in zip(progress, progress[1:]))


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
